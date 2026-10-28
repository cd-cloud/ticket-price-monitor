"""Candidate-scoped date proofs, source union and rate-limit regressions (offline)."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from config_manager import RouteQuery, RouteSegment
from providers import ctrip
from providers.ctrip_tail_candidates import (
    merge_tail_candidates, network_candidate_confirms_date, prepare_network_candidates,
)
from providers.ctrip_tail_discovery import extract_tail_matches_from_network
from providers.ctrip_tail_runner import CtripTailDiscoveryRunner
from providers.response_capture import ResponseStore
from tail_discovery_service import validate_tail_results_for_run
from tail_quotes import select_tail_alternatives
from test_tail_improvements import quote, DATE


def route():
    return RouteQuery("BJS", "CAN", DATE, cabin="economy_plus")


def match(price=700, source="network_exact"):
    return {
        "total_price": price, "details": quote()["flight_details"],
        "detail_source": source, "detail_quality": "complete" if source == "network_exact" else "partial",
        "response_url": "https://flights.ctrip.com/soa2/flight", "query_generation": 1,
    }


def evidence(status="unknown", **overrides):
    return {
        "status": status, "matched": status == "confirmed", "overlay_visible": False,
        "matched_source": "visible_control" if status == "confirmed" else None, **overrides,
    }


def test_mixed_cabin_network_quote_proves_date_without_filtering_onward_cabin():
    candidate = match()
    candidate["details"][1]["cabin"] = "business"
    selected, report = prepare_network_candidates(evidence(), [candidate], route(), "SHA")
    assert report["scope"] == "network_candidates_only"
    assert report["ui_status"] == "unknown"
    assert selected[0]["date_evidence"]["matched_source"] == "network_itinerary"
    assert selected[0]["date_evidence"]["query_generation"] == 1
    candidate["details"][0]["departure_date"] = "2027-06-21"
    assert selected[0]["details"][0]["departure_date"] == DATE


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(detail_source="dom_transit_card"),
    lambda m: m.update(total_price=float("nan")),
    lambda m: m["details"][0].update(departure_date="2027-06-21"),
    lambda m: m["details"][0].update(departure_date=None),
    lambda m: m["details"][1].update(origin="SZX", departure_airport="SZX"),
    lambda m: m["details"][1].update(departure_date=None),
    lambda m: m["details"][1].update(arrival_date=None),
    lambda m: m["details"][1].update(flight_no=None),
    lambda m: m["details"][1].update(departure_time="10:00"),
    lambda m: m["details"].append(deepcopy(m["details"][1])),
])
def test_incomplete_wrong_date_broken_or_dom_itinerary_cannot_prove_date(mutation):
    candidate = match()
    mutation(candidate)
    assert not network_candidate_confirms_date(candidate, route(), "SHA")
    with pytest.raises(RuntimeError, match="date unconfirmed"):
        prepare_network_candidates(evidence(), [candidate], route(), "SHA")


@pytest.mark.parametrize("ui", [
    evidence("mismatch"), evidence(overlay_visible=True), evidence(error="inspection failed"), {},
])
def test_network_proof_never_overrides_conflict_editor_or_inspection_failure(ui):
    with pytest.raises(RuntimeError, match="result date"):
        prepare_network_candidates(ui, [match()], route(), "SHA")


def test_network_dates_remain_scoped_to_each_quote():
    wrong = match(500)
    wrong["details"][0]["departure_date"] = "2027-06-21"
    selected, report = prepare_network_candidates(evidence(), [wrong, match()], route(), "SHA")
    assert [m["total_price"] for m in selected] == [700]
    assert report["confirmed_candidates"] == 1


def test_next_day_connection_is_allowed_and_specific_airport_is_strict():
    candidate = match()
    candidate["details"][1].update(departure_date="2027-06-21", arrival_date="2027-06-21",
                                    departure_time="08:00", arrival_time="10:00")
    assert network_candidate_confirms_date(candidate, route(), "SHA")
    assert not network_candidate_confirms_date(candidate, route(), "PVG")


def test_merge_preserves_prices_sources_and_never_borrows_network_fields():
    network = match(700)
    dom = match(500, "dom_transit_card")
    dom["details"][1]["departure_date"] = None
    merged = merge_tail_candidates([network, deepcopy(network)], [dom])
    assert len(merged) == 2
    assert merged[1]["details"][1]["departure_date"] is None
    assert merged[1]["detail_source"] == "dom_transit_card"
    network["details"].clear()
    assert len(merged[0]["details"]) == 2


def test_partial_cheaper_copy_does_not_replace_verified_same_itinerary():
    verified = quote(price=700)
    verified["validation"] = {"status": "valid"}
    partial = deepcopy(verified)
    partial.update(price=500, validation={"status": "needs_review"})
    cheaper_verified = deepcopy(verified)
    cheaper_verified["price"] = 650
    for rows in ([verified, partial, cheaper_verified], [partial, cheaper_verified, verified]):
        selected = select_tail_alternatives(rows)
        assert len(selected) == 1
        assert selected[0]["price"] == 650
        assert selected[0]["validation"]["status"] == "valid"


@pytest.mark.parametrize("status,generation,accepted", [(200, 1, True), (429, 1, False),
                                                       (500, 1, False), (200, 0, False)])
def test_network_date_evidence_only_uses_successful_current_generation(status, generation, accepted):
    responses = ResponseStore()
    responses.clear()
    responses.append({
        "status": status, "query_generation": generation, "url": "https://example.test/flight",
        "payload": {"flightItineraryList": [{"priceList": [{"adultPrice": 700}]}]},
    })
    legs = match()["details"]
    with patch("providers.ctrip_itinerary.build_itinerary_details", return_value=legs):
        matches = extract_tail_matches_from_network(None, responses, origin="BJS", transfer="SHA", destination="CAN")
    assert bool(matches) == accepted
    if accepted:
        legs[0]["departure_date"] = "bad"
        assert matches[0]["details"][0]["departure_date"] == DATE
        assert matches[0]["query_generation"] == 1


def make_runner(ui):
    automation = SimpleNamespace(provider_runtime=object(), _ctrip_results_date_evidence=AsyncMock(return_value=ui))
    return CtripTailDiscoveryRunner(automation)


def collect(runner, network, dom, *, page_error=None):
    async def run():
        attempt = {}
        with patch("providers.ctrip_tail_runner.extract_tail_matches_from_network", return_value=network), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_page",
                   AsyncMock(return_value=dom, side_effect=page_error)) as parse_page, \
             patch("providers.ctrip_page.dump_datepicker_debug", AsyncMock()):
            result = await runner._collect_candidate_matches(object(), route(), "SHA", [], attempt, "economy")
        return result, attempt, parse_page
    return asyncio.run(run())


def test_unknown_ui_uses_network_date_but_never_parses_date_inferred_dom():
    result, attempt, parser = collect(make_runner(evidence()), [match()], [match(500, "dom_transit_card")])
    assert len(result) == 1
    parser.assert_not_awaited()
    assert attempt["candidate_sources"]["page_status"] == "skipped_unknown_date"


def test_confirmed_page_supplements_network_before_airline_filtering():
    network = match()
    network["details"][0].update(airline="MU", flight_no="MU1001")
    dom = match(800, "dom_transit_card")
    dom["card"] = {"text": "北京 BJS 上海 SHA 广州 CAN"}
    result, attempt, parser = collect(make_runner(evidence("confirmed")), [network], [dom])
    parser.assert_awaited_once()
    assert len(result) == 2
    rows = [{
        **quote(price=m["total_price"], provider="ctrip"), "flight_details": m["details"],
        "raw_payload": {"detail_source": m["detail_source"], "dom_card": m.get("card")},
    } for m in result]
    kept, rejected = validate_tail_results_for_run(
        rows, origin="BJS", transfer="SHA", departure_date=DATE,
        preferred_airlines=["CA"], cabin="economy", filter_scope="first_leg",
    )
    assert len(rejected) == 1
    assert len(kept) == 1 and kept[0]["price"] == 800
    assert kept[0]["validation"]["status"] == "needs_review"
    assert attempt["candidate_sources"]["page"] == 1


def test_dom_failure_does_not_destroy_existing_network_quote():
    result, attempt, _ = collect(make_runner(evidence("confirmed")), [match()], [], page_error=RuntimeError("selector failed"))
    assert len(result) == 1
    assert attempt["candidate_sources"]["page_status"] == "failed"


@pytest.mark.parametrize("error", [RuntimeError("429 rate limit"), asyncio.CancelledError()])
def test_dom_rate_limit_or_cancellation_is_not_swallowed(error):
    with pytest.raises(type(error)):
        collect(make_runner(evidence("confirmed")), [match()], [], page_error=error)


def test_dom_failure_without_network_remains_retryable_failure():
    with pytest.raises(RuntimeError, match="selector failed"):
        collect(make_runner(evidence("confirmed")), [], [], page_error=RuntimeError("selector failed"))


@pytest.mark.parametrize("stage", ["goto", "wait", "dismiss", "warm", "parser", "extract"])
def test_multi_city_rate_limit_never_retries_form(stage):
    async def run():
        runtime = SimpleNamespace(
            throttle=AsyncMock(), wait_for_results=AsyncMock(), dismiss_ctrip_login_overlays=AsyncMock(),
            warm_ctrip_result_data=AsyncMock(), extract_ctrip_multi_city_match_from_network=Mock(return_value=None),
            extract_offer=AsyncMock(),
        )
        page = SimpleNamespace(url="https://flights.ctrip.com/online/list/multi-bjs-sha-sha-can",
                               goto=AsyncMock(return_value=None), wait_for_timeout=AsyncMock())
        error = RuntimeError("429 rate limit")
        actions = {"goto": page.goto, "wait": runtime.wait_for_results,
                   "dismiss": runtime.dismiss_ctrip_login_overlays, "warm": runtime.warm_ctrip_result_data,
                   "parser": runtime.extract_ctrip_multi_city_match_from_network, "extract": runtime.extract_offer}
        actions[stage].side_effect = error
        request = RouteQuery("BJS", "CAN", DATE, route_type="multi_city",
                             segments=[RouteSegment("BJS", "SHA", DATE), RouteSegment("SHA", "CAN", DATE)])
        with pytest.raises(RuntimeError) as caught:
            await ctrip.query_multi_city_offer(runtime, SimpleNamespace(timeout_ms=1000), request, page, ResponseStore())
        assert caught.value is error
        page.goto.assert_awaited_once()  # No landing/form navigation.
    asyncio.run(run())


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("status", [403, 429, 432])
def test_http_navigation_limit_is_detected_even_without_playwright_exception(multi, status):
    async def run():
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=status)), wait_for_timeout=AsyncMock())
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            if multi:
                await ctrip.load_direct_multi_city_offer(object(), SimpleNamespace(timeout_ms=1000), route(), page, [])
            else:
                await ctrip.open_direct_results_page(page, route(), 1000)
        page.wait_for_timeout.assert_not_awaited()
    asyncio.run(run())


def test_ordinary_multi_city_navigation_failure_still_allows_form_fallback():
    page = SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError("navigation timed out")))
    result = asyncio.run(ctrip.load_direct_multi_city_offer(object(), SimpleNamespace(timeout_ms=1000), route(), page, []))
    assert result is None


def test_unclassified_432_uses_stop_policy_without_asserting_flight_absence():
    from browser_retry_policy import looks_like_no_fare, looks_like_rate_limit
    with pytest.raises(RuntimeError) as caught:
        ctrip.raise_if_navigation_limited(SimpleNamespace(status=432))
    assert looks_like_rate_limit(caught.value)
    assert not looks_like_no_fare(caught.value)


@pytest.mark.parametrize("fallback", [False, True])
def test_full_runner_keeps_candidate_date_proof_after_loading_and_backend_retry(fallback):
    import test_tail_improvements as fixtures
    from test_ctrip_query_integrity import response

    async def run():
        runner, automation, _, _ = fixtures.CtripRunnerLifecycleTests().runner()
        automation._ctrip_results_date_evidence.return_value = evidence()
        item = response(day=DATE)
        itinerary = item["payload"]["flightItineraryList"][0]
        onward = deepcopy(itinerary["flightSegments"][0]["flightList"][0])
        onward.update(sequenceNo=2, departureCityCode="SHA", departureAirportCode="SHA",
                      arrivalCityCode="CAN", arrivalAirportCode="CAN",
                      departureDateTime=DATE + "T14:00:00", arrivalDateTime=DATE + "T16:00:00",
                      flightNo="CA2002")
        itinerary["flightSegments"][0]["flightList"].append(onward)
        count = 0

        async def load(runtime, provider, request, page, responses, **kwargs):
            nonlocal count
            count += 1
            if fallback and count == 1:
                raise RuntimeError("navigation timed out")
            responses.clear()
            responses.append({**deepcopy(item), "query_generation": responses.generation})

        with patch("providers.ctrip.query_tail_candidate_offer", side_effect=load), \
             patch.object(runner, "_backend_names_after", return_value=["playwright"]), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_page", AsyncMock()) as dom:
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE,
                                      cabin="economy", passengers=1, candidates=["CAN"])
        assert not result["errors"]
        assert len(result["results"]) == 1
        assert count == (2 if fallback else 1)
        raw = result["results"][0]["raw_payload"]
        assert raw["date_evidence"]["scope"] == "candidate"
        assert raw["date_evidence"]["query_generation"] == raw["query_generation"]
        assert raw["date_evidence"]["ui_status"] == "unknown"
        assert result["attempts"][0]["date_evidence"]["scope"] == "network_candidates_only"
        dom.assert_not_awaited()
        automation._ensure_ctrip_results_date.assert_not_awaited()
    asyncio.run(run())
