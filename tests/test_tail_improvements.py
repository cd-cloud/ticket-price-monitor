"""Deterministic regressions: no browser or live provider access."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from data_storage import PriceRepository
from tail_discovery_service import TailDiscoveryService, validate_tail_results_for_run
from tail_discovery_manager import TailDiscoveryManager
from tail_quotes import (annotate_quote, apply_baselines, build_baseline_catalog, first_leg_key,
                         itinerary_key, public_quote_metadata, quote_terms)
from tail_service import normalize_tail_discovery_request, tail_queue_key
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner

DATE = "2027-06-20"


def quote(destination="CAN", price=700, time="09:00", flight="CA1001", provider="feizhu"):
    observed = datetime.now(timezone.utc).isoformat()
    return {
        "provider": provider, "origin": "BJS", "transfer": "SHA", "destination": destination,
        "departure_date": DATE, "cabin": "economy", "passengers": 1, "currency": "CNY",
        "price": price, "observed_at": observed, "scraped_at": observed,
        "flight_details": [
            {"origin": "BJS", "destination": "SHA", "departure_airport": "PEK", "arrival_airport": "SHA",
             "departure_date": DATE, "departure_time": time, "arrival_time": "11:00", "flight_no": flight,
             "arrival_date": DATE,
             "airline": "CA", "cabin": "economy"},
            {"origin": "SHA", "destination": destination, "departure_airport": "SHA", "arrival_airport": destination,
             "departure_date": DATE, "departure_time": "13:00", "arrival_time": "15:00", "flight_no": "CA2002",
             "arrival_date": DATE,
             "airline": "CA", "cabin": "economy"},
        ], "raw_payload": {"detail_source": "flyai_mcp_api", "detail_quality": "complete"},
    }


def payload(**extra):
    return {"provider": "feizhu", "origin": "BJS", "transfer": "SHA", "departure_date": DATE,
            "cabin": "economy", "candidates": ["CAN"], **extra}


def known_terms(passengers=1):
    return quote_terms("test", passengers, price_basis="per_person", tax_included=True,
                       priced_passengers=passengers, inventory_verified=True, fare_conditions="same-flex")


class TailImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = PriceRepository(Path(self.temp.name) / "db.sqlite")
        self.repo.initialize()
        self.lock = threading.Lock()
        self.service = TailDiscoveryService(
            config=SimpleNamespace(providers={"feizhu": SimpleNamespace(enabled=True)}, default_currency="CNY"),
            config_manager=SimpleNamespace(load_credentials=lambda **kw: {}), repository=self.repo,
            run_lock=self.lock, queue_file=Path(self.temp.name) / "queue.json", safe_batch_size=3,
        )

    def use_client(self, offers, crash_after=None, delay=0):
        called = []
        class Client:
            async def query_tail_baseline(self, provider, **kwargs):
                return None
            async def discover_tail_routes(self, provider, **kwargs):
                attempts, results = [], []
                for code in kwargs["candidates"]:
                    if kwargs["cancel_checker"]():
                        break
                    called.append(code)
                    if delay:
                        await asyncio.sleep(delay)
                    matches = deepcopy(offers.get(code, []))
                    results.extend(matches)
                    attempt = {"destination": code, "status": "matched" if matches else "no_match",
                               "phase": "matched" if matches else "route_absent", "elapsed_seconds": 1}
                    attempts.append(attempt)
                    kwargs["progress_callback"]({"stage": "candidate_finished", "destination": code,
                                                 "attempt": attempt, "results": results})
                    if crash_after == code:
                        raise RuntimeError("simulated process failure after checkpoint")
                return {"results": results, "attempts": attempts, "errors": []}
        @asynccontextmanager
        async def client(*args):
            yield Client()
        self.service._tail_discovery_client = client
        return called

    def run_service(self, **kwargs):
        return asyncio.run(self.service.run(payload(**kwargs)))

    def save_job(self, job_id, result=None, request=None):
        self.repo.upsert_tail_discovery_job({"job_id": job_id, "status": "interrupted", "payload": request or payload(),
                                            "result": result, "progress": (result or {}).get("progress") or {}})

    def test_filter_before_minimum_and_keep_multiple_flights(self):
        self.use_client({"CAN": [quote(price=600, time="02:00"), quote(price=700), quote(price=800, time="10:00", flight="CA1003")]})
        result = self.run_service(departure_time_start="08:00", departure_time_end="12:00")
        self.assertEqual([700, 800], [r["price"] for r in result["results"]])
        self.assertEqual(2, len(self.repo.fetch_tail_discovery_results()))

    def test_wrong_cabin_does_not_hide_more_expensive_valid_offer(self):
        low = quote(price=500)
        low["flight_details"][0]["cabin"] = "business"
        self.use_client({"CAN": [low, quote(price=800)]})
        result = self.run_service()
        self.assertEqual([800], [r["price"] for r in result["results"]])
        self.assertEqual("matched", result["report"]["attempts"][0]["status"])

    def test_same_timestamp_distinct_itineraries_do_not_overwrite(self):
        first, second = quote(), quote(time="10:00", flight="CA1010")
        second["observed_at"] = first["observed_at"]
        a = self.repo.insert_tail_discovery_result(first)
        b = self.repo.insert_tail_discovery_result(second)
        self.assertNotEqual(a, b)
        self.assertEqual(a, self.repo.insert_tail_discovery_result(first))

    def test_fresh_explore_does_not_skip_previous_success(self):
        calls = self.use_client({"CAN": [quote()]})
        self.run_service(_job_id="one")
        self.run_service(_job_id="two")
        self.assertEqual(["CAN", "CAN"], calls)

    def test_resume_only_queries_unfinished_after_crash(self):
        calls = self.use_client({"CAN": [quote()], "SZX": [quote("SZX")]}, crash_after="CAN")
        request = payload(candidates=["CAN", "SZX"])
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            asyncio.run(self.service.run({**request, "_job_id": "original"}))
        self.assertFalse(self.lock.locked())
        self.assertEqual(1, len(self.repo.fetch_tail_discovery_results()))
        self.save_job("original", request=request)
        calls = self.use_client({"SZX": [quote("SZX")]})
        result = asyncio.run(self.service.run({**request, "_job_id": "resume1", "mode": "resume", "source_job_id": "original"}))
        self.assertEqual(["SZX"], calls)
        self.assertEqual(2, result["saved"])
        self.assertEqual(2, len(self.repo.fetch_tail_discovery_results()))

    def test_resume_rejects_changed_passenger_count_and_releases_lock(self):
        self.use_client({"CAN": [quote()]})
        result = self.run_service(_job_id="original")
        self.save_job("original", result)
        with self.assertRaises(ValueError):
            self.run_service(mode="resume", source_job_id="original", passengers=2)
        self.assertFalse(self.lock.locked())

    def test_refresh_queries_known_destinations_even_with_negative_cache(self):
        self.use_client({"CAN": [quote()]})
        self.run_service(_job_id="original")
        calls = self.use_client({"CAN": [quote(price=900)]})
        result = self.run_service(mode="refresh", candidates=["CAN", "SZX"])
        self.assertEqual(["CAN"], calls)
        self.assertEqual([900], [r["price"] for r in result["results"]])

    def test_no_results_refresh_does_not_start_provider(self):
        calls = self.use_client({})
        with self.assertRaises(ValueError):
            self.run_service(mode="refresh")
        self.assertEqual([], calls)

    def test_candidate_budget_preserves_pending_and_can_resume(self):
        calls = self.use_client({"CAN": [quote()], "SZX": [quote("SZX")]})
        result = self.run_service(_job_id="budget", candidates=["CAN", "SZX"], max_candidates=1)
        self.assertEqual(["CAN"], calls)
        self.assertEqual("candidate_budget", result["stop_reason"])
        self.assertEqual(1, result["queue"]["report"]["pending"])
        self.save_job("budget", result, payload(candidates=["CAN", "SZX"], max_candidates=1))
        calls = self.use_client({"SZX": [quote("SZX")]})
        result2 = self.run_service(mode="resume", source_job_id="budget", candidates=["SZX", "CAN"], max_candidates=1)
        self.assertEqual(["SZX"], calls)
        self.assertEqual(2, result2["saved"])

    def test_target_budget_stops_after_valid_first_flight(self):
        calls = self.use_client({"CAN": [quote()], "SZX": [quote("SZX")]})
        result = self.run_service(candidates=["CAN", "SZX"], target_results=1)
        self.assertEqual(["CAN"], calls)
        self.assertEqual("target_reached", result["stop_reason"])

    def test_wall_clock_budget_interrupts_slow_candidate(self):
        self.use_client({"CAN": [quote()]}, delay=.2)
        result = self.run_service(max_minutes=.0002)
        self.assertEqual("time_budget", result["stop_reason"])
        self.assertEqual([], self.repo.fetch_tail_discovery_results())
        self.assertFalse(self.lock.locked())

    def test_atomic_checkpoint_rolls_back_all_writes(self):
        record = {"job_id": "j", "queue_key": "q", "origin": "BJS", "transfer": "SHA", "destination": "CAN",
                  "departure_date": DATE, "cabin": "economy", "status": "matched"}
        with patch.object(self.repo, "insert_tail_discovery_attempt", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                self.repo.checkpoint_tail_candidate(root_job_id="j", fingerprint="q", attempt=record, results=[quote()])
        self.assertEqual([], self.repo.fetch_tail_discovery_results())
        self.assertEqual([], self.repo.tail_checkpoints("j"))

    def test_checkpoint_callback_is_idempotent(self):
        record = {"job_id": "j", "queue_key": "q", "origin": "BJS", "transfer": "SHA", "destination": "CAN",
                  "departure_date": DATE, "cabin": "economy", "status": "matched"}
        for _ in range(2):
            self.repo.checkpoint_tail_candidate(root_job_id="j", fingerprint="q", attempt=record, results=[quote()])
        self.assertEqual(1, len(self.repo.fetch_tail_discovery_results()))
        self.assertEqual(1, len(self.repo.fetch_tail_discovery_attempts()))

    def test_query_fingerprint_is_order_invariant_but_passenger_sensitive(self):
        self.assertEqual(tail_queue_key(candidates=["CAN", "SZX"], passengers=1),
                         tail_queue_key(candidates=["SZX", "CAN"], passengers=1))
        self.assertNotEqual(tail_queue_key(passengers=1), tail_queue_key(passengers=2))

    def test_invalid_input_never_leaks_run_lock(self):
        with self.assertRaises(ValueError):
            self.run_service(departure_date="bad")
        self.assertFalse(self.lock.locked())
        self.use_client({})
        self.run_service()

    def test_recheck_only_returns_selected_itinerary_and_marks_timestamp(self):
        selected = quote()
        self.use_client({"CAN": [selected, quote(time="10:00", flight="CA1010")]})
        result = self.run_service(mode="recheck", expected_itinerary_key=itinerary_key(selected))
        self.assertEqual(1, result["saved"])
        self.assertEqual(selected["observed_at"], result["results"][0]["raw_payload"]["rechecked_at"])

    def test_cooldown_blocks_new_queries_and_releases_lock(self):
        self.repo.set_tail_cooldown("feizhu", (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
        calls = self.use_client({})
        with self.assertRaisesRegex(RuntimeError, "冷却"):
            self.run_service()
        self.assertFalse(self.lock.locked())
        self.assertEqual([], calls)

    def test_infrastructure_failure_does_not_become_no_route_evidence(self):
        for code, phase in [("CAN", "query_failed"), ("SZX", "no_via_transfer")]:
            for _ in range(3):
                self.repo.insert_tail_discovery_attempt({"queue_key": "q", "provider": "feizhu", "origin": "BJS",
                    "transfer": "SHA", "destination": code, "departure_date": DATE, "cabin": "economy",
                    "status": "failed" if code == "CAN" else "no_match", "phase": phase})
        order = self.service._prioritize_candidates_with_history(["SZX", "CAN"], origin="BJS", transfer="SHA",
                         preferred_airlines=[], provider="feizhu", cabin="economy")
        # Legacy partial searches cannot establish route absence either.
        self.assertEqual(["SZX", "CAN"], order)

    def test_unknown_candidates_get_exploration_slot(self):
        for code in ["CAN", "SZX"]:
            self.repo.insert_tail_discovery_attempt({"queue_key": "q", "provider": "feizhu", "origin": "BJS",
                "transfer": "SHA", "destination": code, "departure_date": DATE, "cabin": "economy",
                "status": "matched", "phase": "matched"})
        order = self.service._prioritize_candidates_with_history(["CAN", "SZX", "KMG"], origin="BJS", transfer="SHA",
                         preferred_airlines=[], provider="feizhu", cabin="economy")
        self.assertEqual("KMG", order[1])

    def cache_negative(self, destination):
        from tail_discovery_service import negative_cache_signature
        entry = dict(provider="feizhu", origin="BJS", transfer="SHA", destination=destination,
                     departure_date=DATE, cabin="economy", passengers=1, preferred_airlines=[])
        signature = negative_cache_signature(**entry)
        self.repo.cache_tail_negative({**entry, "signature": signature, "phase": "no_via_transfer",
                                      "observed_at": datetime.now(timezone.utc).isoformat(),
                                      "expires_at": "2099-01-01T00:00:00+00:00"})
        return signature

    def test_positive_refresh_clears_old_negative_cache(self):
        signature = self.cache_negative("CAN")
        calls = self.use_client({"CAN": [quote()]})
        self.run_service(ignore_cache=True)
        self.assertEqual(set(), self.repo.active_negative_cache_codes({"CAN": signature}, now=datetime.now(timezone.utc).isoformat()))
        self.run_service()
        self.assertEqual(["CAN", "CAN"], calls)

    def test_feizhu_ignores_legacy_negative_cache_for_targeted_cli_search(self):
        self.cache_negative("CAN")
        calls = self.use_client({"CAN": [quote()]})
        result = self.run_service()
        self.assertEqual(["CAN"], calls)
        self.assertEqual(1, result["saved"])

    def test_resumed_refresh_still_bypasses_negative_cache(self):
        self.repo.insert_tail_discovery_result(quote())
        self.repo.insert_tail_discovery_result(quote("SZX"))
        self.cache_negative("SZX")
        self.use_client({"CAN": [quote()]})
        result = self.run_service(_job_id="refresh1", mode="refresh", candidates=["CAN", "SZX"], max_candidates=1)
        self.save_job("refresh1", result, payload(mode="refresh", candidates=["CAN", "SZX"], max_candidates=1))
        calls = self.use_client({"SZX": [quote("SZX")]})
        resumed = self.run_service(mode="resume", source_job_id="refresh1", candidates=["CAN", "SZX"], max_candidates=1)
        self.assertEqual(["SZX"], calls)
        self.assertEqual("refresh", resumed["execution_mode"])

    def test_baseline_rate_limit_stops_before_candidate_queries(self):
        client = SimpleNamespace(query_tail_baseline=AsyncMock(side_effect=RuntimeError("429 rate limit")),
                                 discover_tail_routes=AsyncMock())
        @asynccontextmanager
        async def context(*args):
            yield client
        self.service._tail_discovery_client = context
        result = self.run_service()
        self.assertEqual("rate_limited", result["stop_reason"])
        self.assertIsNotNone(self.repo.tail_cooldown("feizhu"))
        client.discover_tail_routes.assert_not_awaited()


class QuoteSemanticsTests(unittest.TestCase):
    def test_unknown_terms_do_not_claim_savings(self):
        result = quote()
        apply_baselines([result], {"price": 1000, "observed_at": result["observed_at"]})
        self.assertIsNone(result["savings_amount"])
        self.assertEqual("reference_only", result["raw_payload"]["baselines"]["market"]["status"])

    def test_two_comparable_baselines(self):
        result = quote()
        result["raw_payload"]["quote_terms"] = known_terms()
        baseline = {"price": 1000, "observed_at": result["observed_at"], "quote_terms": known_terms()}
        apply_baselines([result], {"market": baseline, "same_flights": {first_leg_key(result): {**baseline, "price": 1200}}})
        self.assertEqual(300, result["savings_amount"])
        self.assertEqual(500, result["raw_payload"]["baselines"]["same_flight"]["savings_amount"])

    def test_currency_tax_party_and_stale_baselines_not_comparable(self):
        for change in [{"currency": "USD"}, {"tax_included": False}, {"priced_passengers": 2}]:
            with self.subTest(change=change):
                result = quote()
                result["raw_payload"]["quote_terms"] = known_terms()
                baseline = {"price": 1000, "observed_at": result["observed_at"], "quote_terms": {**known_terms(), **change}}
                apply_baselines([result], baseline)
                self.assertIsNone(result["savings_amount"])
        baseline = {"price": 1000, "observed_at": "2020-01-01T00:00:00+00:00", "quote_terms": known_terms()}
        apply_baselines([result], baseline)
        self.assertIsNone(result["savings_amount"])

    def test_requested_multiple_passengers_are_not_verified_inventory(self):
        result = quote()
        result["passengers"] = 3
        annotate_quote(result)
        self.assertFalse(result["raw_payload"]["quote_terms"]["inventory_verified"])
        self.assertEqual(3, result["raw_payload"]["quote_terms"]["requested_passengers"])
        self.assertIsNone(result["raw_payload"]["quote_terms"]["priced_passengers"])

    def test_expiry_is_derived_from_observation_not_read_time(self):
        result = quote()
        annotate_quote(result, ttl_minutes=20)
        future = datetime.now(timezone.utc) + timedelta(minutes=21)
        self.assertEqual("stale", public_quote_metadata(result, now=future)["freshness"])
        self.assertEqual("fresh", public_quote_metadata(result)["freshness"])

    def test_baseline_catalog_honors_time_cabin_and_airline(self):
        early, valid, wrong = quote(price=600, time="02:00"), quote(price=900), quote(price=500)
        for r in [early, valid, wrong]:
            r["flight_details"] = r["flight_details"][:1]
        wrong["flight_details"][0]["cabin"] = "business"
        catalog = build_baseline_catalog([early, valid, wrong], provider="feizhu", departure_date=DATE,
            cabin="economy", passengers=1, observed_at=valid["observed_at"], time_start="08:00", preferred_airlines=["CA"])
        self.assertEqual(900, catalog["market"]["price"])
        self.assertEqual(1, len(catalog["same_flights"]))

    def test_missing_cabin_is_review_not_verified(self):
        result = quote()
        result["flight_details"][0].pop("cabin")
        kept, _ = validate_tail_results_for_run([result], origin="BJS", transfer="SHA", departure_date=DATE,
                                               preferred_airlines=[], cabin="economy")
        self.assertEqual("needs_review", kept[0]["validation"]["status"])


class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_match_always_publishes_progress_and_cools_down(self):
        runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        events = []
        with patch("providers.feizhu_flyai_adapter.search_flights", AsyncMock(return_value=[])), \
             patch.object(runner, "_cancellable_wait", AsyncMock()) as wait:
            await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                             candidates=["CAN", "SZX"], progress_callback=events.append)
        self.assertEqual(2, len([event for event in events if event["stage"] == "candidate_finished"]))
        wait.assert_awaited_once()

    async def test_rate_limit_stops_remaining_candidates(self):
        runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        with patch("providers.feizhu_flyai_adapter.search_flights", AsyncMock(side_effect=RuntimeError("429 rate limit"))) as search:
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                                      candidates=["CAN", "SZX"])
        self.assertEqual(1, search.await_count)
        self.assertEqual("rate_limited", result["attempts"][0]["phase"])

    async def test_runner_returns_all_matches(self):
        runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        with patch("providers.feizhu_flyai_adapter.search_flights", AsyncMock(return_value=[quote(price=600), quote(price=700, time="10:00")])):
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                                      candidates=["CAN"])
        self.assertEqual(2, len(result["results"]))

class CtripRunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def runner(self):
        from providers.ctrip_tail_runner import CtripTailDiscoveryRunner
        page = SimpleNamespace(url="https://flights.ctrip.com/online/list/oneway-bjs-can", on=lambda *args: None)
        context = object()
        metadata = SimpleNamespace(browser_backend="chrome", to_payload=lambda: {"browser_backend": "chrome"})
        automation = SimpleNamespace(
            app_config=SimpleNamespace(providers={"ctrip": SimpleNamespace(timeout_ms=1000)}, default_currency="CNY"),
            provider_runtime=object(), TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS=120, _new_page=AsyncMock(return_value=(page, context)),
            _session_metadata_for_context=lambda ctx: metadata,
            _ctrip_verification_detected=AsyncMock(return_value=False),
            _wait_for_ctrip_verification_continue=AsyncMock(return_value=True),
            _save_storage_state_if_allowed=AsyncMock(), _start_ctrip_tail_trace=AsyncMock(return_value=False),
            _stop_ctrip_tail_trace=AsyncMock(return_value=None), _close_context_quietly=AsyncMock(),
            _ensure_ctrip_results_date=AsyncMock(), _warm_ctrip_tail_result_data=AsyncMock(),
            _ctrip_results_date_evidence=AsyncMock(return_value={
                "matched": True, "status": "confirmed", "matched_source": "visible_control", "overlay_visible": False}),
            _dump_ctrip_tail_diagnostics=AsyncMock(return_value={}), _tail_cancellable_wait=AsyncMock(),
        )
        return CtripTailDiscoveryRunner(automation), automation, page, context

    async def test_ctrip_no_match_publishes_and_cools_down(self):
        runner, automation, _, _ = self.runner()
        events = []
        with patch("providers.ctrip.query_tail_candidate_offer", AsyncMock()), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_network", return_value=[]), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_page", AsyncMock(return_value=[])), \
             patch("providers.ctrip_page.has_no_results_notice", AsyncMock(return_value=True)):
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                                      candidates=["CAN", "SZX"], progress_callback=events.append)
        self.assertEqual(2, len([event for event in events if event["stage"] == "candidate_finished"]))
        self.assertEqual(["no_match", "no_match"], [a["status"] for a in result["attempts"]])
        automation._tail_cancellable_wait.assert_awaited_once()
        automation._close_context_quietly.assert_awaited_once()

    async def test_ctrip_preserves_all_network_alternatives(self):
        runner, _, _, _ = self.runner()
        matches = [{"total_price": p, "details": quote(price=p)["flight_details"], "detail_source": "network_exact"}
                   for p in (600, 700)]
        with patch("providers.ctrip.query_tail_candidate_offer", AsyncMock()), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_network", return_value=matches), \
             patch("providers.ctrip_tail_runner.extract_tail_matches_from_page", AsyncMock(return_value=[])):
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                                      candidates=["CAN"])
        self.assertEqual([600, 700], [r["price"] for r in result["results"]])

    async def test_verification_gate_waits_for_human_and_resumes(self):
        runner, automation, page, context = self.runner()
        automation._ctrip_verification_detected.return_value = True
        events = []
        outcome = await runner._verification_gate(page, context, {"destination": "CAN"}, events.append, None, None)
        self.assertTrue(outcome)
        self.assertEqual(["needs_verification", "verification_continued"], [e["stage"] for e in events])
        automation._wait_for_ctrip_verification_continue.assert_awaited_once()

    async def test_ctrip_rate_limit_does_not_switch_backend(self):
        runner, automation, _, _ = self.runner()
        with patch("providers.ctrip.query_tail_candidate_offer", AsyncMock(side_effect=RuntimeError("429 rate limit"))), \
             patch.object(runner, "_retry_candidate_with_backend_fallback", AsyncMock()) as fallback:
            result = await runner.run(origin="BJS", transfer="SHA", departure_date=DATE, cabin="economy", passengers=1,
                                      candidates=["CAN", "SZX"])
        self.assertEqual("rate_limited", result["attempts"][0]["phase"])
        self.assertEqual(1, len(result["attempts"]))
        fallback.assert_not_awaited()

class ExtraQuoteRegressionTests(unittest.TestCase):
    def test_hhmm_and_hhmmss_share_first_leg_identity(self):
        first, second = quote(time="09:30"), quote(time="09:30:00")
        self.assertEqual(first_leg_key(first), first_leg_key(second))
        second["flight_details"] = second["flight_details"][:1]
        result = build_baseline_catalog([second], provider="feizhu", departure_date=DATE, cabin="economy",
            passengers=1, observed_at=second["observed_at"], time_start="09:00", time_end="10:00")
        self.assertIsNotNone(result)

    def test_empty_explicit_candidate_list_does_not_expand_to_defaults(self):
        with self.assertRaises(ValueError):
            normalize_tail_discovery_request(payload(candidates=[]))

    def test_network_parser_returns_all_prices_before_filtering(self):
        from providers.ctrip_tail_discovery import extract_tail_matches_from_network
        response = [{"payload": {"flightItineraryList": [{"priceList": [{"p":600},{"p":700}]}]}}]
        with patch("providers.ctrip_itinerary.total_price", side_effect=lambda item:item["p"]), \
             patch("providers.ctrip_itinerary.build_itinerary_details", return_value=quote()["flight_details"]):
            matches = extract_tail_matches_from_network(None, response, origin="BJS", transfer="SHA", destination="CAN")
        self.assertEqual([600,700], [m["total_price"] for m in matches])

    def test_unconfirmed_recheck_expires_original_atomically_without_claiming_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = PriceRepository(Path(tmp)/"test.db"); repo.initialize()
            original = annotate_quote(quote())
            result_id = repo.insert_tail_discovery_result(original)
            record = {"job_id":"j","queue_key":"q","origin":"BJS","transfer":"SHA","destination":"CAN",
                      "departure_date":DATE,"cabin":"economy","status":"no_match"}
            repo.checkpoint_tail_candidate(root_job_id="j", fingerprint="q", attempt=record, results=[], recheck_result_id=result_id)
            row = repo.fetch_tail_result(result_id)
            self.assertEqual("stale", public_quote_metadata(row)["freshness"])
            self.assertEqual("inconclusive", row["raw_payload"]["recheck_outcome"]["status"])


class CliCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_child_exit(self):
        import os
        import sys
        from providers.feizhu_flyai_adapter import _run_flyai_command_async
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp)/"pid"
            script = "import os,time,pathlib;pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(30)"
            task = asyncio.create_task(_run_flyai_command_async([sys.executable,"-c",script],30))
            for _ in range(100):
                if marker.exists(): break
                await asyncio.sleep(.01)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            if sys.platform != "win32":
                with self.assertRaises(ProcessLookupError): os.kill(pid,0)

    async def test_incomplete_cli_response_is_failure_not_no_route(self):
        import subprocess
        from providers import feizhu_flyai_adapter as adapter
        with patch.object(adapter,"_find_flyai_executable",return_value=["flyai"]), \
             patch.object(adapter,"_run_flyai_command_async",AsyncMock(return_value=subprocess.CompletedProcess([],0,'{"error":"busy"}',''))):
            with self.assertRaisesRegex(RuntimeError,"incomplete"):
                await adapter.search_flights(origin="BJS",destination="CAN",departure_date=DATE)
