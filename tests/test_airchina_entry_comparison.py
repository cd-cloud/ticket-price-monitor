"""Replay public visible evidence from two entry paths; no live airline I/O.

Duplicated samples below test the waiter's rules, not a claim of two real
observations, causal isolation, full inventory or unattended query success.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config_manager import RouteQuery
from providers.airchina_parser import parse_reference_fares
from providers.airchina_result_wait import wait_for_results


def samples():
    return json.loads((Path(__file__).parent / "fixtures" /
                       "airchina_entry_comparison.json").read_text())


def test_same_visible_request_can_have_different_entry_outcomes():
    data = samples()
    form, direct = (data["snapshots"][key] for key in ("form", "direct"))
    for key in ("url", "schema", "route_text", "date_text", "trip_type", "passengers_text"):
        assert form[key] == direct[key]
    route = RouteQuery(**data["request"])
    success = parse_reference_fares(form, route)
    empty = parse_reference_fares(direct, route)
    assert len(success["offers"]) == 16
    assert success["rejected_rows"] == 0
    flight = next(o for o in success["offers"] if o["flight_no"] == "CA1549")
    assert (flight["departure_time"], flight["arrival_time"], flight["reference_fare"]) == (
        "16:30", "18:45", 600,
    )
    assert flight["tax_included"] is False and flight["total_price"] is None
    assert empty["reason"] == "page_reports_no_results"
    assert empty["offers"] == [] and not empty["coverage_complete"]
    assert not success["quote_saved"] and not empty["quote_saved"]


def test_empty_direct_page_invalidates_a_prior_form_fare_sample():
    data = samples()
    form, direct = (data["snapshots"][key] for key in ("form", "direct"))
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=[form, direct]),
                           wait_for_timeout=AsyncMock())
    report = asyncio.run(wait_for_results(
        page, RouteQuery(**data["request"]), expected_url=form["url"],
        timeout_ms=500, poll_ms=250,
    ))
    assert report["state"] == "query_page_unverified"
    assert report["reference_preview"]["offers"] == []
    assert report["wait_evidence"]["stable_samples"] == 0
    assert report["wait_evidence"]["last_observation"]["parser_reason"] == "page_reports_no_results"


def test_recorded_form_sample_satisfies_existing_stable_wait_rules():
    data = samples()
    form = data["snapshots"]["form"]
    page = SimpleNamespace(evaluate=AsyncMock(return_value=form), wait_for_timeout=AsyncMock())
    report = asyncio.run(wait_for_results(
        page, RouteQuery(**data["request"]), expected_url=form["url"],
        timeout_ms=500, poll_ms=250,
    ))
    assert report["state"] == "reference_fares_observed"
    assert report["wait_evidence"]["polls"] == 2
    assert len(report["reference_preview"]["offers"]) == 16
    assert not report["reference_preview"]["parser_ready"]
