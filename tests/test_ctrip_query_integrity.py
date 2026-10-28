"""Query attribution regressions. No real browser, CLI or external requests."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from browser_automation import BrowserAutomation
from browser_query_result import OfferDetails
from config_manager import RouteQuery, RouteSegment
from providers import ctrip, ctrip_itinerary, ctrip_network_parser as parser, ctrip_result
from providers.response_capture import ResponseStore, attach_response_listeners, begin_response_batch
from data_storage import PriceRepository
from tail_discovery_service import TailDiscoveryService


DAY = "2027-01-15"


def route(**kwargs):
    return RouteQuery(**{"origin": "BJS", "destination": "SHA", "departure_date": DAY,
                         "cabin": "economy", **kwargs})


def response(*, cabin="Y", airline="CA", amount=500, flight="CA1234", day=DAY, airport="PEK"):
    return {"url": "https://flights.ctrip.com/soa2/flight", "status": 200, "query_generation": 1,
            "payload": {"flightItineraryList": [{
                "flightSegments": [{"segmentNo": 1, "flightList": [{
                    "sequenceNo": 1, "marketAirlineCode": airline, "flightNo": flight,
                    "departureCityCode": "BJS", "departureAirportCode": airport,
                    "arrivalCityCode": "SHA", "arrivalAirportCode": "SHA",
                    "departureDateTime": day + "T10:00:00", "arrivalDateTime": day + "T12:00:00",
                }]}],
                "priceList": [{"adultPrice": amount, "adultTax": 50, "cabin": cabin}],
            }]}}


def selected(responses, request=None):
    match = parser.extract_single_route_match_from_network(responses, request or route())
    assert match is not None
    return OfferDetails(price=match["total_price"], matched_quote=match)


@pytest.mark.parametrize("requested,actual", [
    ("business", "Y"), ("economy", "C"), ("premium_economy", "Y"),
    ("first", "C"), ("economy", None), ("economy", "Y_C"),
])
def test_wrong_or_unconfirmed_cabin_is_not_a_matching_fare(requested, actual):
    assert parser.extract_single_route_match_from_network([response(cabin=actual)], route(cabin=requested)) is None


@pytest.mark.parametrize("requested,actual", [
    ("economy", "Y"), ("economy_plus", "S"), ("economy_plus", "Y"),
    ("business_first", "C"), ("business_first", "F"),
])
def test_supported_cabin_groups(requested, actual):
    assert parser.extract_single_route_match_from_network([response(cabin=actual)], route(cabin=requested))


def test_airline_and_cabin_are_filtered_before_selecting_minimum():
    offers = [response(cabin="Y", amount=200), response(cabin="C", airline="MU", flight="MU1234", amount=300),
              response(cabin="C", amount=900)]
    match = parser.extract_single_route_match_from_network(offers, route(cabin="business", preferred_airlines=["CA"]))
    assert match["total_price"] == 950
    assert match["details"][0]["airline"] == "CA"


@pytest.mark.parametrize("origin,airport,accepted", [
    ("BJS", "PEK", True), ("BJS", "PKX", True), ("PEK", "PEK", True),
    ("PEK", "PKX", False), ("PKX", "PEK", False), ("PEK", None, False),
])
def test_city_and_explicit_airport_semantics(origin, airport, accepted):
    assert bool(parser.extract_single_route_match_from_network(
        [response(airport=airport)], route(origin=origin))) == accepted


@pytest.mark.parametrize("status,day", [(500, DAY), (429, DAY), (200, "2027-01-16")])
def test_error_response_and_wrong_departure_date_rejected(status, day):
    item = response(day=day)
    item["status"] = status
    assert parser.extract_single_route_match_from_network([item], route()) is None


def test_calendar_with_matching_dates_but_wrong_route_is_not_a_quote():
    item = {"payload": {"origin": "CAN", "destination": "SZX", "priceList": [
        {"departDate": "/Date(1800000000000+0800)/", "totalPrice": 399}]}}
    expected = parser.ctrip_date_to_iso(item["payload"]["priceList"][0]["departDate"])
    assert parser.extract_single_route_match_from_network([item], route(departure_date=expected)) is None


def test_late_cheaper_response_cannot_reassign_flight_details():
    rows = [response()]
    offer = selected(rows)
    rows.append(response(amount=300, flight="CA9999"))
    rows[0]["payload"].clear()  # The selected quote is also detached from mutable raw payloads.
    outcome = ctrip_result.build_single_route_outcome(route(), offer, "https://example.test", "title", rows)
    assert outcome.price == 550
    assert outcome.flight_details[0]["flight_no"] == "CA1234"
    assert outcome.departure_time == "10:00"
    assert outcome.extra_payload["selected_quote"]["query_generation"] == 1


def test_price_tampering_or_request_change_fails_closed():
    offer = selected([response()])
    offer.price = 300
    with pytest.raises(RuntimeError, match="independently"):
        ctrip_result.build_single_route_outcome(route(), offer, "", "")
    offer.price = 550
    with pytest.raises(RuntimeError, match="conditions"):
        ctrip_result.build_single_route_outcome(route(cabin="business"), offer, "", "")


def test_visible_only_price_is_not_promoted_by_a_later_network_match():
    offer = OfferDetails(price=550, row_text="CA1234 商务舱 ¥550")
    with pytest.raises(RuntimeError, match="unverified"):
        ctrip_result.build_single_route_outcome(route(), offer, "", "", [response()])


def round_trip_response():
    item = response()
    itinerary = item["payload"]["flightItineraryList"][0]
    inbound = deepcopy(itinerary["flightSegments"][0])
    inbound["segmentNo"] = 2
    flight = inbound["flightList"][0]
    flight.update(departureCityCode="SHA", departureAirportCode="SHA",
                  arrivalCityCode="BJS", arrivalAirportCode="PEK",
                  departureDateTime="2027-01-20 10:00:00", arrivalDateTime="2027-01-20 12:00:00")
    itinerary["flightSegments"].append(inbound)
    return item


def test_return_date_checked_on_actual_itinerary():
    item = round_trip_response()
    assert parser.extract_single_route_match_from_network([item], route(return_date="2027-01-20"))
    assert parser.extract_single_route_match_from_network([item], route(return_date="2027-01-21")) is None


def test_multi_city_fare_is_filtered_and_kept_with_its_details():
    request = route(route_type="multi_city", segments=[
        RouteSegment("BJS", "SHA", DAY), RouteSegment("SHA", "BJS", "2027-01-20")])
    good = round_trip_response()
    bad = deepcopy(good)
    bad["payload"]["flightItineraryList"][0]["priceList"][0].update(cabin="C", adultPrice=200)
    match = parser.extract_multi_city_match_from_network([bad, good], request)
    assert match["total_price"] == 550
    runtime = SimpleNamespace(build_itinerary_summary=lambda _: "BJS-SHA-BJS")
    outcome = ctrip_result.build_multi_city_outcome(
        runtime, request, OfferDetails(price=550, matched_quote=match), "", "", [bad])
    assert len(outcome.flight_details) == 2
    assert outcome.price == 550


def test_broken_chain_cannot_match_just_by_first_and_last_city():
    item = response()
    flights = item["payload"]["flightItineraryList"][0]["flightSegments"][0]["flightList"]
    flights.append(deepcopy(flights[0]))
    flights[0].update(arrivalCityCode="KMG", arrivalAirportCode="KMG")
    flights[1].update(departureCityCode="CAN", departureAirportCode="CAN")
    assert parser.extract_single_route_match_from_network([item], route()) is None


class EventPage:
    def __init__(self):
        self.handlers = {}
    def on(self, event, handler):
        self.handlers[event] = handler


def test_capture_rejects_old_requests_and_old_body_decoding():
    async def run():
        page, store = EventPage(), ResponseStore()
        await attach_response_listeners(page, store)
        old_request = object()
        page.handlers["request"](old_request)
        begin_response_batch(store)
        old = SimpleNamespace(request=old_request)
        assert page.handlers["response"](old) is None
        request = object()
        page.handlers["request"](request)
        decoding_started, release = asyncio.Event(), asyncio.Event()
        async def body():
            decoding_started.set()
            await release.wait()
            return {"old": True}
        slow = SimpleNamespace(request=request, url="https://flights.ctrip.com/soa2/flight",
                               status=200, headers={"content-type": "application/json"}, json=body)
        task = page.handlers["response"](slow)
        await decoding_started.wait()
        begin_response_batch(store)
        release.set()
        await task
        assert store == []
        fresh_request = object()
        page.handlers["request"](fresh_request)
        fresh = SimpleNamespace(request=fresh_request, url=slow.url, status=200, headers=slow.headers,
                                json=AsyncMock(return_value={"current": True}))
        await page.handlers["response"](fresh)
        assert store[0]["payload"] == {"current": True}
        assert store[0]["query_generation"] == store.generation
        assert not store.requests
    asyncio.run(run())


def test_cabin_url_correction_rotates_response_generation():
    async def run():
        store = ResponseStore()
        store.append(response())
        page = SimpleNamespace(url="https://flights.ctrip.com/online/list/oneway-bjs-sha?cabin=y", goto=AsyncMock())
        await ctrip.ensure_results_cabin_url(page, route(cabin="business"), 1000, store)
        assert store == []
        assert store.generation == 1
        assert "cabin=c" in page.goto.await_args.args[0]
    asyncio.run(run())


def test_tail_page_loading_does_not_require_an_ordinary_selected_fare():
    async def run():
        runtime = SimpleNamespace(throttle=AsyncMock(), wait_for_results=AsyncMock(),
            dismiss_ctrip_login_overlays=AsyncMock(), warm_ctrip_result_data=AsyncMock(),
            extract_offer=AsyncMock(side_effect=AssertionError("must not pre-filter mixed tail fares")))
        page = SimpleNamespace(url="https://flights.ctrip.com/online/list/oneway-bjs-sha",
                               title=AsyncMock(return_value="results"))
        with patch("providers.ctrip.open_direct_results_page", AsyncMock(return_value=True)), \
             patch("providers.ctrip.ctrip_page.has_login_overlay", AsyncMock(return_value=False)):
            result = await ctrip.query_tail_candidate_offer(runtime, SimpleNamespace(timeout_ms=1000), route(), page, ResponseStore())
        assert result[0] is None
        runtime.extract_offer.assert_not_awaited()
    asyncio.run(run())


def test_ordinary_extraction_carries_snapshot_without_using_dom_fallback():
    async def run():
        automation = BrowserAutomation.__new__(BrowserAutomation)
        automation._extract_ctrip_offer = AsyncMock(side_effect=AssertionError("no authoritative DOM fallback"))
        offer = await automation._extract_offer(SimpleNamespace(name="ctrip"), route(), None, [response()])
        assert offer.matched_quote["total_price"] == offer.price == 550
        with pytest.raises(RuntimeError, match="unverified"):
            await automation._extract_offer(SimpleNamespace(name="ctrip"), route(), None, [])
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["route_absent", "no_via_transfer", "search_inconclusive"])
def test_partial_tail_search_is_unknown_not_cached_and_resume_retries(tmp_path, phase):
    repo = PriceRepository(tmp_path / "test.db")
    repo.initialize()
    service = TailDiscoveryService(
        config=SimpleNamespace(providers={"ctrip": SimpleNamespace(enabled=True)}, default_currency="CNY"),
        config_manager=SimpleNamespace(load_credentials=lambda **kw: {}),
        repository=repo, run_lock=threading.Lock(), queue_file=tmp_path / "queue.json",
        safe_batch_size=3,
    )
    calls = []
    class Client:
        async def discover_tail_routes(self, provider, **kwargs):
            calls.extend(kwargs["candidates"])
            return {"results": [], "errors": [], "attempts": [
                {"destination": "CAN", "status": "no_match", "phase": phase},
            ]}
    @asynccontextmanager
    async def client(*args):
        yield Client()
    service._tail_discovery_client = client
    request = {"provider": "ctrip", "origin": "BJS", "transfer": "SHA",
               "departure_date": DAY, "cabin": "economy", "candidates": ["CAN"]}
    first = asyncio.run(service.run({**request, "_job_id": "first"}))
    assert first["report"]["attempts"][0]["status"] == "failed"
    assert not first["candidate_policy"]["negative_cache_enabled"]
    with repo.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tail_negative_cache").fetchone()[0] == 0
        assert conn.execute("SELECT outcome FROM tail_search_outcomes").fetchone()[0] == "unknown"
        assert conn.execute("SELECT count(*) FROM tail_route_edges").fetchone()[0] == 0
    repo.upsert_tail_discovery_job({"job_id": "first", "status": "finished",
                                    "payload": request, "result": first})
    asyncio.run(service.run({**request, "_job_id": "second", "mode": "resume", "source_job_id": "first"}))
    assert calls == ["CAN", "CAN"]


def test_unmatched_roundtrip_airline_or_cabin_is_not_accepted():
    item = round_trip_response()
    price = item["payload"]["flightItineraryList"][0]["priceList"][0]
    price["priceUnitList"] = [{"flightSeatList": [
        {"segmentNo": 1, "sequenceNo": 1, "cabinClass": "Y"},
        {"segmentNo": 2, "sequenceNo": 1, "cabinClass": "C"},
    ]}]
    assert parser.extract_single_route_match_from_network([item], route(return_date="2027-01-20")) is None


def test_rate_limit_from_direct_results_is_not_retried_as_form_failure():
    async def run():
        runtime = SimpleNamespace(wait_for_results=AsyncMock(side_effect=RuntimeError("429 rate limit")))
        with patch("providers.ctrip.open_direct_results_page", AsyncMock(return_value=True)):
            with pytest.raises(RuntimeError, match="429"):
                await ctrip.load_direct_route_offer(runtime, SimpleNamespace(timeout_ms=1000), route(),
                                                   SimpleNamespace(), ResponseStore())
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["one_way", "round_trip", "multi_city", "multi_city_asymmetric"])
def test_provider_entry_point_carries_selected_quote_end_to_end(mode):
    async def run():
        request = route()
        item = response()
        if mode == "round_trip":
            request.return_date = "2027-01-20"
            item = round_trip_response()
        elif mode.startswith("multi_city"):
            request.route_type = "multi_city"
            end = "CAN" if mode == "multi_city_asymmetric" else "BJS"
            request.segments = [RouteSegment("BJS", "SHA", DAY), RouteSegment("SHA", end, "2027-01-20")]
            item = round_trip_response()
            if end == "CAN":
                item["payload"]["flightItineraryList"][0]["flightSegments"][1]["flightList"][0].update(
                    arrivalCityCode="CAN", arrivalAirportCode="CAN")
        automation = BrowserAutomation.__new__(BrowserAutomation)
        async def warm(page, responses):
            responses.append(deepcopy(item))
        runtime = SimpleNamespace(
            throttle=AsyncMock(), wait_for_results=AsyncMock(), dismiss_ctrip_login_overlays=AsyncMock(),
            warm_ctrip_result_data=warm, extract_offer=automation._extract_offer,
            ensure_ctrip_results_date=AsyncMock(), offer_details_type=OfferDetails,
            extract_ctrip_multi_city_match_from_network=parser.extract_multi_city_match_from_network,
            build_itinerary_summary=lambda _: "test",
        )
        page = SimpleNamespace(
            url="https://flights.ctrip.com/online/list/test",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), title=AsyncMock(return_value="results"),
        )
        outcome = await ctrip.query(runtime, SimpleNamespace(name="ctrip", timeout_ms=1000), request, page, ResponseStore())
        assert outcome.price == 550
        assert outcome.extra_payload["validation"]["status"] == "valid"
        assert outcome.flight_details[0]["cabin"] == "economy"
        assert len(outcome.flight_details) == (1 if mode == "one_way" else 2)
    asyncio.run(run())
