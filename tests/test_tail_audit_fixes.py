"""Regressions from the 2026-10-27 audit; all provider I/O is mocked."""
from contextlib import asynccontextmanager
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

import test_tail_improvements as helpers
from providers import feizhu_flyai_adapter as adapter
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from tail_discovery_service import negative_cache_signature, validate_tail_results_for_run
from tail_quotes import annotate_quote, itinerary_key, public_quote_metadata
from tail_service import normalize_tail_discovery_request, tail_queue_key


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TailImprovementTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.repo, self.service = self.h.repo, self.h.service
        self.calls = []
        self.wait = patch.object(FeizhuTailDiscoveryRunner, "_cancellable_wait", AsyncMock())
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def run_service(self, **kw):
        return self.h.run_service(**kw)

    def search(self, handler):
        async def fake(**kw):
            self.calls.append(kw)
            return handler(kw)
        mock = patch.object(adapter, "search_flights", side_effect=fake)
        mock.start()
        self.addCleanup(mock.stop)

    def validate(self, item, **kw):
        kept, rejected = validate_tail_results_for_run(
            [item], origin="BJS", transfer="SHA", departure_date=helpers.DATE,
            preferred_airlines=kw.pop("preferred_airlines", []), cabin="economy", **kw)
        return item["validation"], kept

    def test_filtered_rate_limit_stops_next_batch_and_remains_retryable(self):
        calls = []
        class Client:
            async def discover_tail_routes(self, provider, **kw):
                calls.extend(kw["candidates"])
                code = kw["candidates"][0]
                return {"results": [helpers.quote(code, price=900)],
                        "attempts": [{"destination": code, "status": "matched",
                                      "phase": "rate_limited", "error": "429"}]}
        @asynccontextmanager
        async def client(*args):
            yield Client()
        self.service._tail_discovery_client = client
        self.service.safe_batch_size = 1
        result = self.run_service(candidates=["CAN", "SZX"], max_price=500)
        self.assertEqual(calls, ["CAN"])
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertIsNotNone(self.repo.tail_cooldown("feizhu"))
        attempt = result["report"]["attempts"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["phase"], "rate_limited")
        self.assertEqual(attempt["stop_reason"], "rate_limited")
        self.assertEqual(attempt["error"], "429")

    def test_retained_quote_survives_enrichment_rate_limit_and_target_does_not_override(self):
        def response(kw):
            if kw.get("transport_no"):
                raise RuntimeError("429 Too Many Requests")
            return [] if kw["journey_type"] == "1" else [helpers.quote()]
        self.search(response)
        result = self.run_service(target_results=1)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertIsNotNone(self.repo.tail_cooldown("feizhu"))
        self.assertEqual(result["report"]["attempts"][0]["stop_reason"], "rate_limited")

    def test_chronology_rejects_reversed_connection(self):
        q = helpers.quote()
        q["flight_details"][1]["departure_time"] = "10:00"
        validation, kept = self.validate(q)
        self.assertEqual(validation["status"], "invalid")
        self.assertFalse(kept)

    def test_chronology_rejects_year_long_connection(self):
        q = helpers.quote()
        q["flight_details"][1].update(departure_date="2028-06-20", arrival_date="2028-06-20")
        self.assertEqual(self.validate(q)[0]["status"], "invalid")

    def test_missing_identifiers_or_times_are_review_only(self):
        for field in ("flight_no", "departure_time", "arrival_time", "arrival_date"):
            with self.subTest(field=field):
                q = helpers.quote()
                q["flight_details"][0].pop(field)
                validation, kept = self.validate(q)
                self.assertEqual(validation["status"], "needs_review")
                self.assertEqual(len(kept), 1)

    def test_invalid_time_or_date_not_valid(self):
        for field, value in (("arrival_time", "25:01"), ("arrival_date", "2027-02-30")):
            q = helpers.quote()
            q["flight_details"][0][field] = value
            self.assertEqual(self.validate(q)[0]["status"], "invalid")

    def test_malformed_leg_structure_is_rejected_without_crashing(self):
        for details in ({"origin": "BJS"}, [None, None], []):
            with self.subTest(details=details):
                q = helpers.quote()
                q["flight_details"] = details
                self.assertEqual(self.validate(q)[0]["status"], "invalid")

    def test_cross_midnight_connection_preserves_real_dates(self):
        q = helpers.quote(time="23:00")
        q["flight_details"][0].update(arrival_time="01:00", arrival_date="2027-06-21")
        q["flight_details"][1].update(departure_date="2027-06-21", arrival_date="2027-06-21",
                                     departure_time="03:00", arrival_time="05:00")
        self.assertEqual(self.validate(q)[0]["status"], "valid")
        self.assertEqual(q["flight_details"][1]["departure_date"], "2027-06-21")

    def test_short_or_long_connections_need_review_not_guaranteed_valid(self):
        for changes in ({"departure_time": "11:15"},
                        {"departure_date": "2027-06-22", "arrival_date": "2027-06-22"}):
            q = helpers.quote()
            q["flight_details"][1].update(changes)
            self.assertEqual(self.validate(q)[0]["status"], "needs_review")

    def test_unknown_timezone_not_compared_as_same_local_clock(self):
        q = helpers.quote()
        q.update(origin="JFK", transfer="LHR", destination="CDG")
        q["flight_details"][0].update(origin="JFK", departure_airport="JFK", destination="LHR", arrival_airport="LHR")
        q["flight_details"][1].update(origin="LHR", departure_airport="LHR", destination="CDG", arrival_airport="CDG")
        kept, _ = validate_tail_results_for_run(
            [q], origin="JFK", transfer="LHR", departure_date=helpers.DATE,
            preferred_airlines=[], cabin="economy")
        self.assertEqual(kept[0]["validation"]["status"], "needs_review")
        self.assertTrue(any("时区" in w for w in kept[0]["validation"]["warnings"]))

    def test_explicit_timezone_offsets_support_valid_international_times(self):
        q = helpers.quote()
        q.update(origin="JFK", transfer="LHR", destination="CDG")
        q["flight_details"][0].update(origin="JFK", departure_airport="JFK", destination="LHR", arrival_airport="LHR",
            departure_time=f"{helpers.DATE}T01:00:00-04:00", arrival_time=f"{helpers.DATE}T13:00:00+01:00")
        q["flight_details"][1].update(origin="LHR", departure_airport="LHR", destination="CDG", arrival_airport="CDG",
            departure_time=f"{helpers.DATE}T15:00:00+01:00", arrival_time=f"{helpers.DATE}T17:00:00+02:00")
        kept, _ = validate_tail_results_for_run(
            [q], origin="JFK", transfer="LHR", departure_date=helpers.DATE, preferred_airlines=[], cabin="economy")
        self.assertEqual(kept[0]["validation"]["status"], "valid")

    def test_missing_dates_are_not_inferred_from_query(self):
        q = helpers.quote()
        q["flight_details"][0].pop("departure_date")
        self.assertEqual(self.validate(q)[0]["status"], "needs_review")

    def test_adapter_preserves_iso_dates_and_timezone_offsets(self):
        leg = adapter._segment_to_flight_detail({
            "depDateTime": "2027-06-20T09:00:00+08:00",
            "arrDateTime": "2027-06-20T11:00:00+08:00",
        })
        self.assertEqual(leg["arrival_date"], helpers.DATE)
        self.assertEqual(leg["departure_date"], helpers.DATE)
        q = helpers.quote()
        q["flight_details"][0].update({k: leg[k] for k in (
            "departure_date", "arrival_date", "departure_time", "arrival_time")})
        self.assertEqual(self.validate(q)[0]["status"], "valid")

    def test_recheck_uses_original_flight_but_omission_is_inconclusive_and_resumable(self):
        original = annotate_quote(helpers.quote())
        old_id = self.repo.insert_tail_discovery_result(original)
        responses = [helpers.quote(flight="CA1010", time="10:00")]
        self.search(lambda kw: [] if kw["journey_type"] == "1"
                    else adapter.FlightSearchResults(deepcopy(responses), metadata={"coverage": "unknown"}))
        request = dict(mode="recheck", source_result_id=old_id, expected_itinerary_key=itinerary_key(original),
                       _job_id="recheck1")
        result = self.run_service(**request)
        transit = [c for c in self.calls if c["journey_type"] == "2"]
        self.assertEqual(transit[0]["transport_no"], "CA1001")
        self.assertEqual(len(self.calls), 2)  # Do not enrich a different itinerary.
        row = self.repo.fetch_tail_result(old_id)
        self.assertEqual(row["raw_payload"]["recheck_outcome"]["status"], "inconclusive")
        self.assertEqual(public_quote_metadata(row)["freshness"], "stale")
        self.assertEqual(result["report"]["attempts"][0]["status"], "failed")
        self.h.save_job("recheck1", result, helpers.payload(**request))
        responses[:] = [helpers.quote(price=800)]
        resumed = self.run_service(**{**request, "mode": "resume", "source_job_id": "recheck1", "_job_id": "recheck2"})
        self.assertEqual(resumed["saved"], 1)
        self.assertEqual(self.repo.fetch_tail_result(old_id)["raw_payload"]["recheck_outcome"]["status"], "found")

    def test_empty_recheck_also_inconclusive(self):
        q = annotate_quote(helpers.quote())
        old_id = self.repo.insert_tail_discovery_result(q)
        self.search(lambda kw: [])
        self.run_service(mode="recheck", source_result_id=old_id, expected_itinerary_key=itinerary_key(q))
        self.assertEqual(self.repo.fetch_tail_result(old_id)["raw_payload"]["recheck_outcome"]["status"], "inconclusive")

    def test_network_error_recheck_stays_failed(self):
        q = annotate_quote(helpers.quote())
        old_id = self.repo.insert_tail_discovery_result(q)
        def response(kw):
            if kw["journey_type"] == "2":
                raise RuntimeError("network unreachable")
            return []
        self.search(response)
        self.run_service(mode="recheck", source_result_id=old_id, expected_itinerary_key=itinerary_key(q))
        self.assertEqual(self.repo.fetch_tail_result(old_id)["raw_payload"]["recheck_outcome"]["status"], "failed")

    def test_filter_then_enrich_correct_first_flight(self):
        eligible = helpers.quote(flight="CA1003", time="09:00", price=700)
        def response(kw):
            if kw["journey_type"] == "2":
                return [helpers.quote(time="02:00", price=600), deepcopy(eligible)]
            if kw.get("transport_no") == "CA1003":
                q = deepcopy(eligible)
                q["flight_details"] = q["flight_details"][:1]
                return [q]
            return []
        self.search(response)
        result = self.run_service(departure_time_start="08:00", departure_time_end="12:00")
        extras = [c["transport_no"] for c in self.calls if c.get("transport_no")]
        self.assertEqual(extras, ["CA1003"])
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["results"][0]["raw_payload"]["baselines"]["same_flight"]["price"], 700)

    def test_no_enrichment_if_filtered_or_invalid(self):
        for kind in ("price", "chronology"):
            with self.subTest(kind=kind):
                self.calls.clear()
                q = helpers.quote(price=900)
                if kind == "chronology":
                    q["flight_details"][1]["departure_time"] = "01:00"
                with patch.object(adapter, "search_flights", AsyncMock(side_effect=[[], [q]])) as search:
                    result = self.run_service(max_price=500 if kind == "price" else None)
                self.assertEqual(search.await_count, 2)
                self.assertEqual(result["saved"], 0)

    def test_baseline_budget_is_run_wide_across_batches_and_can_be_disabled(self):
        self.service.safe_batch_size = 1
        def response(kw):
            return [] if kw["journey_type"] == "1" else [
                helpers.quote(kw["destination"], flight="CA1001" if kw["destination"] == "CAN" else "CA1003")]
        self.search(response)
        with patch("tail_discovery_service.asyncio.sleep", AsyncMock()):
            self.run_service(candidates=["CAN", "SZX"], max_baseline_queries=1)
        self.assertEqual(len([c for c in self.calls if c.get("transport_no")]), 1)
        self.calls.clear()
        self.run_service(max_baseline_queries=0)
        self.assertFalse(any(c.get("transport_no") for c in self.calls))

    def test_open_multiple_cities_share_budget_and_same_first_flight_lookup(self):
        rows = [helpers.quote("CAN"), helpers.quote("SZX"),
                helpers.quote("HGH", flight="CA1003"), helpers.quote("KMG", flight="CA1005")]
        self.search(lambda kw: [] if kw["journey_type"] == "1" else deepcopy(rows))
        result = self.run_service(search_scope="open", candidates=[], max_baseline_queries=2)
        self.assertEqual(result["saved"], 4)
        self.assertEqual([c["transport_no"] for c in self.calls if c.get("transport_no")], ["CA1001", "CA1003"])
        self.assertEqual(result["report"]["attempts"][0]["baseline_queries"], 2)

    def test_dedup_and_alternative_cap_before_enrichment(self):
        q = helpers.quote()
        duplicate = deepcopy(q)
        self.search(lambda kw: [] if kw["journey_type"] == "1" else [deepcopy(q), deepcopy(duplicate)])
        result = self.run_service()
        self.assertEqual(result["saved"], 1)
        self.assertEqual(len([c for c in self.calls if c.get("transport_no")]), 1)

    def test_resume_cumulative_statistics_agree_with_checkpoint_queue(self):
        self.h.use_client({"CAN": [helpers.quote()], "SZX": [helpers.quote("SZX")]})
        first = self.run_service(_job_id="one", candidates=["CAN", "SZX"], max_candidates=1)
        self.h.save_job("one", first, helpers.payload(candidates=["CAN", "SZX"], max_candidates=1))
        result = self.run_service(mode="resume", source_job_id="one", candidates=["CAN", "SZX"])
        self.assertEqual(result["report"]["pending"], 0)
        self.assertEqual(result["progress"]["pending"], 0)
        self.assertEqual(result["report"]["attempted"], 2)
        self.assertEqual(result["queue"]["completed"], 2)
        self.assertEqual(result["newly_saved"], 1)
        self.assertEqual(result["metrics"]["queried_candidates"], 1)

    def test_completed_directed_resume_does_not_query_or_show_pending(self):
        calls = self.h.use_client({"CAN": [helpers.quote()]})
        first = self.run_service(_job_id="one")
        self.h.save_job("one", first)
        self.run_service(mode="resume", source_job_id="one")
        self.assertEqual(calls, ["CAN"])

    def test_first_leg_scope_accepts_mixed_tail_cabin_all_legs_rejects(self):
        q = helpers.quote()
        q["flight_details"][1]["cabin"] = "business"
        self.assertEqual(self.validate(deepcopy(q))[0]["status"], "valid")
        self.assertEqual(self.validate(deepcopy(q), filter_scope="all_legs")[0]["status"], "invalid")

    def test_tail_only_airline_match_no_longer_satisfies_first_leg(self):
        q = helpers.quote(flight="MU1001")
        q["flight_details"][0]["airline"] = "MU"
        self.assertEqual(self.validate(q, preferred_airlines=["CA"])[0]["status"], "invalid")

    def test_all_legs_requires_every_leg_airline(self):
        q = helpers.quote()
        q["flight_details"][1].update(airline="MU", flight_no="MU2002")
        self.assertEqual(self.validate(deepcopy(q), preferred_airlines=["CA"])[0]["status"], "valid")
        self.assertEqual(self.validate(deepcopy(q), preferred_airlines=["CA"], filter_scope="all_legs")[0]["status"], "invalid")

    def test_flyai_first_leg_scope_omits_upstream_cabin_but_all_scope_keeps_it(self):
        self.search(lambda kw: [] if kw["journey_type"] == "1" else [helpers.quote()])
        self.run_service(max_baseline_queries=0)
        transit = next(c for c in self.calls if c["journey_type"] == "2")
        self.assertEqual(transit["cabin"], "any")
        self.assertNotIn("--seat-class-name", adapter._build_cli_args(
            origin="BJS", destination="CAN", departure_date=helpers.DATE, cabin=transit["cabin"]))
        self.calls.clear()
        self.run_service(filter_scope="all_legs", max_baseline_queries=0)
        self.assertEqual(next(c for c in self.calls if c["journey_type"] == "2")["cabin"], "economy")

    def test_scope_changes_cannot_resume_prior_results(self):
        self.h.use_client({"CAN": [helpers.quote()]})
        first = self.run_service(_job_id="one", filter_scope="all_legs")
        self.h.save_job("one", first, helpers.payload(filter_scope="all_legs"))
        with self.assertRaisesRegex(ValueError, "查询条件已改变"):
            self.run_service(mode="resume", source_job_id="one", filter_scope="first_leg")

    def test_scope_and_validation_version_isolate_negative_cache(self):
        args = dict(provider="ctrip", origin="BJS", transfer="SHA", destination="CAN")
        self.assertNotEqual(negative_cache_signature(**args), tail_queue_key(**args))
        self.assertNotEqual(negative_cache_signature(**args),
                            negative_cache_signature(**args, filter_scope="all_legs"))

    def test_legacy_checkpoint_requires_new_exploration_not_mixed_rules(self):
        record = {"job_id": "old", "queue_key": "legacy", "origin": "BJS", "transfer": "SHA",
                  "destination": "CAN", "departure_date": helpers.DATE, "cabin": "economy",
                  "status": "matched", "provider": "feizhu"}
        self.repo.checkpoint_tail_candidate(root_job_id="old", fingerprint="legacy",
                                            attempt=record, results=[helpers.quote()])
        self.h.save_job("old")
        calls = self.h.use_client({})
        with self.assertRaisesRegex(ValueError, "校验规则已升级"):
            self.run_service(mode="resume", source_job_id="old")
        self.assertEqual(calls, [])

    def test_request_defaults_and_bounds(self):
        request = normalize_tail_discovery_request(helpers.payload())
        self.assertEqual(request.filter_scope, "first_leg")
        self.assertEqual(request.max_baseline_queries, 3)
        for changes in ({"filter_scope": "any_leg"}, {"max_baseline_queries": -1},
                        {"max_baseline_queries": 21}, {"max_baseline_queries": 1.5},
                        {"max_baseline_queries": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_tail_discovery_request(helpers.payload(**changes))
