"""Offline evidence and progressive loading tests; no browser or provider needed."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config_manager import RouteQuery
from providers.ctrip_date_evidence import collect_date_evidence, date_value_matches
from providers.ctrip_page import ensure_results_departure_date
from providers.ctrip_result_wait import wait_for_progressive_results
from providers.response_capture import ResponseStore, attach_response_listeners


DAY, RETURN = "2027-01-05", "2027-01-10"


class Nodes:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)
    async def count(self):
        return len(self.nodes)
    def nth(self, index):
        return self.nodes[index]


class Node:
    def __init__(self, value=None, text="", visible=True, children=()):
        self.value, self.text, self.visible, self.children = value, text, visible, children
    async def is_visible(self):
        return self.visible
    async def input_value(self, **kwargs):
        if self.value is None:
            raise RuntimeError("not an input")
        return self.value
    async def inner_text(self, **kwargs):
        return self.text
    def locator(self, selector):
        assert selector == "input"
        return Nodes(self.children)


class DatePage:
    def __init__(self, *, departure=(), returning=(), widgets=(), body="", url=None, arbitrary=()):
        self.departure, self.returning, self.widgets = departure, returning, widgets
        self.body, self.arbitrary = Node(text=body), arbitrary
        self.url = url or f"https://flights.ctrip.com/online/list/test?depdate={DAY}"
    def locator(self, selector):
        if selector == "body":
            return self.body
        if selector.startswith(".modifyDate.depart-date"):
            return Nodes(self.departure)
        if selector.startswith(".modifyDate.return-date"):
            return Nodes(self.returning)
        if selector == "form#searchForm .flt-date .modifyDate":
            return Nodes(self.widgets)
        if selector == "input":
            raise AssertionError("must not use arbitrary page inputs as selected date evidence")
        return Nodes()


@pytest.mark.parametrize("value,expected", [
    ("2027-01-05", True), ("2027/1/5", True), ("周二 1月5日", True),
    ("2028-01-05", False), ("2027-01-051", False), ("11月5日", False),
    ("2027-01-05_2027-01-10", False), ("2027-01-05T10:00:00", True),
    ("2027年01月05日", True), ("2026年01月05日", False),
])
def test_date_matching_does_not_accept_wrong_year_or_substring(value, expected):
    assert date_value_matches(value, DAY) == expected


def test_arbitrary_input_and_calendar_strip_are_not_date_confirmation():
    page = DatePage(arbitrary=[Node(value=DAY)], body=f"低价日历 {DAY} ¥600 {RETURN} ¥800")
    evidence = asyncio.run(collect_date_evidence(page, DAY))
    assert not evidence["matched"]
    assert evidence["status"] == "unknown"


def test_correct_input_cannot_override_conflicting_result_header():
    page = DatePage(departure=[Node(value=DAY)], body="出发日期：2027-01-06")
    evidence = asyncio.run(collect_date_evidence(page, DAY))
    assert evidence["status"] == "mismatch"
    assert not evidence["matched"]


def test_hidden_selected_control_does_not_confirm_date():
    page = DatePage(departure=[Node(value=DAY, visible=False)])
    assert asyncio.run(collect_date_evidence(page, DAY))["status"] == "unknown"


def test_open_calendar_blocks_even_a_matching_input():
    page = DatePage(departure=[Node(value=DAY)])
    evidence = asyncio.run(collect_date_evidence(page, DAY, overlay_visible=True))
    assert not evidence["matched"]
    assert evidence["matched_source"] == "calendar_open"


def test_url_alone_and_empty_body_do_not_confirm_date():
    evidence = asyncio.run(collect_date_evidence(DatePage(), DAY))
    assert evidence["url_date_matched"]
    assert not evidence["matched"]


def test_yearless_visible_date_needs_independent_year_evidence():
    page = DatePage(departure=[Node(value="1月5日")], url="https://flights.ctrip.com/online/list/test")
    assert asyncio.run(collect_date_evidence(page, DAY))["status"] == "unknown"
    page.url += f"?depdate={DAY}"
    assert asyncio.run(collect_date_evidence(page, DAY))["matched"]


def test_visible_control_container_reads_child_input():
    page = DatePage(widgets=[Node(children=[Node(value=DAY)])])
    assert asyncio.run(collect_date_evidence(page, DAY))["matched"]


def test_round_trip_requires_correct_outbound_and_return_dates():
    page = DatePage(widgets=[Node(value=DAY), Node(value=RETURN)],
                    url=f"https://flights.ctrip.com/online/list/test?depdate={DAY}_{RETURN}")
    assert asyncio.run(collect_date_evidence(page, DAY, return_date=RETURN))["matched"]
    page.widgets[1].value = "2027-01-11"
    evidence = asyncio.run(collect_date_evidence(page, DAY, return_date=RETURN))
    assert not evidence["matched"]
    assert evidence["roles"]["return"]["status"] == "mismatch"


def test_return_field_or_return_url_is_not_outbound_proof():
    page = DatePage(returning=[Node(value=DAY)],
                    url=f"https://flights.ctrip.com/online/list/test?depdate=2027-01-04_{DAY}")
    evidence = asyncio.run(collect_date_evidence(page, DAY))
    assert not evidence["matched"]
    assert not evidence["url_date_matched"]


def test_missing_return_evidence_stays_unknown_even_with_correct_url():
    page = DatePage(departure=[Node(value=DAY)],
                    url=f"https://flights.ctrip.com/online/list/test?depdate={DAY}_{RETURN}")
    evidence = asyncio.run(collect_date_evidence(page, DAY, return_date=RETURN))
    assert not evidence["matched"]
    assert evidence["roles"]["return"]["status"] == "unknown"


def test_labelled_visible_header_can_confirm_date_but_wrong_url_conflicts():
    page = DatePage(body=f"单程：北京上海01月05日周二 出发日期：{DAY}")
    assert asyncio.run(collect_date_evidence(page, DAY))["matched"]
    page.url = page.url.replace(DAY, "2027-01-06")
    assert asyncio.run(collect_date_evidence(page, DAY))["status"] == "mismatch"


@pytest.mark.parametrize("page,kind", [
    (DatePage(), "unconfirmed"),
    (DatePage(departure=[Node(value="2027-01-06")]), "mismatch"),
])
def test_result_guard_distinguishes_unknown_and_wrong_date(page, kind):
    request = RouteQuery("BJS", "SHA", DAY)
    with patch("providers.ctrip_page.is_datepicker_overlay_visible", AsyncMock(return_value=False)), \
         patch("providers.ctrip_page.dump_datepicker_debug", AsyncMock()):
        with pytest.raises(RuntimeError, match=f"date {kind}"):
            asyncio.run(ensure_results_departure_date(page, request, 1000))


@pytest.mark.parametrize("body,overlay,accepted", [
    ("", False, True), ("出发日期：2027-01-06", False, False), ("", True, False),
])
def test_exact_quote_can_fill_unknown_ui_date_but_never_override_conflict(body, overlay, accepted):
    from test_ctrip_query_integrity import response, selected
    request = RouteQuery("BJS", "SHA", DAY)
    offer = selected([response(day=DAY)], request)
    page = DatePage(body=body)
    with patch("providers.ctrip_page.is_datepicker_overlay_visible", AsyncMock(return_value=overlay)), \
         patch("providers.ctrip_page.dump_datepicker_debug", AsyncMock()):
        if accepted:
            evidence = asyncio.run(ensure_results_departure_date(page, request, 1000, offer=offer))
            assert evidence["matched_source"] == "network_itinerary"
            assert evidence["ui_status"] == "unknown"
        else:
            with pytest.raises(RuntimeError):
                asyncio.run(ensure_results_departure_date(page, request, 1000, offer=offer))


def test_unknown_ui_does_not_allow_unbound_or_wrong_date_quote():
    from test_ctrip_query_integrity import response, selected
    request = RouteQuery("BJS", "SHA", DAY)
    wrong = selected([response()])
    with patch("providers.ctrip_page.is_datepicker_overlay_visible", AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="conditions"):
            asyncio.run(ensure_results_departure_date(DatePage(), request, 1000, offer=wrong))


def flight_response():
    return {"status": 200, "payload": {"flightItineraryList": [{"priceList": [{"adultPrice": 500}]}]}}


class LoadingPage:
    def __init__(self, responses, *, on_wait=None, height=600, loading=False, cards=1, stuck=False):
        self.responses, self.on_wait = responses, on_wait
        self.y, self.height, self.loading, self.cards, self.stuck = 0, height, loading, cards, stuck
        self.waits = self.scrolls = 0
    async def evaluate(self, script):
        if script.startswith("window.scrollBy"):
            self.scrolls += 1
            if not self.stuck:
                self.y = min(self.y + 400, self.height - 600)
            return None
        return dict(y=self.y, height=self.height, viewport=600, cards=self.cards, loading=self.loading)
    async def wait_for_timeout(self, ms):
        assert ms == 1000
        self.waits += 1
        if self.on_wait:
            self.on_wait(self)


def test_first_itinerary_does_not_end_wait_before_late_batch():
    responses = ResponseStore()
    responses.append(flight_response())
    def late(page):
        if page.waits == 2:
            responses.append(flight_response())
    page = LoadingPage(responses, on_wait=late)
    report = asyncio.run(wait_for_progressive_results(page, responses))
    assert len(responses) == 2
    assert page.waits >= 5
    assert report["reason"] == "quiet"
    assert not report["coverage_complete"]


def test_lazy_scrolling_continues_despite_existing_itinerary():
    responses = ResponseStore()
    responses.append(flight_response())
    page = LoadingPage(responses, height=1800)
    report = asyncio.run(wait_for_progressive_results(page, responses))
    assert page.scrolls >= 3
    assert page.y == 1200
    assert report["reason"] == "quiet"


@pytest.mark.parametrize("loading,cards,has_flight", [(True, 1, True), (False, 0, False)])
def test_loading_or_no_data_has_bounded_wait(loading, cards, has_flight):
    responses = ResponseStore()
    if has_flight:
        responses.append(flight_response())
    else:
        responses.append({"payload": {"flightItineraryList": []}})
    page = LoadingPage(responses, loading=loading, cards=cards)
    report = asyncio.run(wait_for_progressive_results(page, responses, max_polls=5))
    assert report["reason"] == "wait_budget"
    assert page.waits == 4
    assert not report["coverage_complete"]


def test_stuck_scroll_is_reported_as_stalled_not_complete():
    responses = ResponseStore()
    responses.append(flight_response())
    page = LoadingPage(responses, height=1800, stuck=True)
    report = asyncio.run(wait_for_progressive_results(page, responses))
    assert report["reason"] == "scroll_stalled"
    assert not report["coverage_complete"]


def test_unchanged_settled_generation_is_reused_but_new_response_restarts_wait():
    responses = ResponseStore()
    responses.append(flight_response())
    page = LoadingPage(responses)
    asyncio.run(wait_for_progressive_results(page, responses))
    waits = page.waits
    assert asyncio.run(wait_for_progressive_results(page, responses))["polls"] == 0
    assert page.waits == waits
    responses.append(flight_response())
    assert asyncio.run(wait_for_progressive_results(page, responses))["polls"] >= 4
    responses.clear()
    assert responses.loading_evidence is None


def test_active_request_or_body_decoding_prevents_quiet_exit():
    async def run():
        responses = ResponseStore()
        callbacks = {}
        await attach_response_listeners(SimpleNamespace(on=lambda event, callback: callbacks.update({event: callback})), responses)
        class Request:
            url = "https://example.test/flight-search"
            resource_type = "fetch"
        request = Request()
        callbacks["request"](request)
        assert responses.inflight == 1
        release = asyncio.Event()
        async def body():
            await release.wait()
            return flight_response()["payload"]
        response = SimpleNamespace(request=request, url=request.url, status=200,
            headers={"content-type": "application/json"}, json=body)
        task = callbacks["response"](response)
        assert responses.inflight == 1
        page = LoadingPage(responses)
        report = await wait_for_progressive_results(page, responses, max_polls=5)
        assert report["reason"] == "wait_budget"
        release.set()
        await task
        assert responses.inflight == 0
        assert (await wait_for_progressive_results(page, responses))["reason"] == "quiet"
    asyncio.run(run())


def test_result_wait_propagates_cancellation():
    page = LoadingPage(ResponseStore())
    page.wait_for_timeout = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wait_for_progressive_results(page, page.responses))


def test_loading_state_read_error_never_counts_as_quiet():
    responses = ResponseStore()
    responses.append(flight_response())
    page = LoadingPage(responses)
    page.evaluate = AsyncMock(side_effect=RuntimeError("page state unavailable"))
    report = asyncio.run(wait_for_progressive_results(page, responses, max_polls=4))
    assert report["reason"] == "wait_budget"
    assert report["polls"] == 4
    assert not report["coverage_complete"]
