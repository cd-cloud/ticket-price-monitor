"""Offline history feedback, dynamic planning, atomicity and explicit quota tests."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_tail_improvements as helpers
from providers import feizhu_flyai_adapter as adapter
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from tail_service import OPEN_DESTINATION, normalize_tail_discovery_request
from tail_history import history_context, rank_candidates, context_similarity
from tail_discovery_manager import TailDiscoveryManager
from tail_call_budget import TailCallBudget, ProviderCallBudgetExceeded


class HistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TailImprovementTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.repo, self.service = self.h.repo, self.h.service
        self.calls = []
        self.rows = [helpers.quote("CAN")]
        self.directed = {}
        self.fail_destination = None
        async def search(**kw):
            self.calls.append(kw)
            if kw["journey_type"] == "1":
                return []
            if kw.get("destination") == self.fail_destination and self.fail_destination:
                raise RuntimeError("network failure")
            return deepcopy(self.directed.get(kw["destination"], self.rows)
                            if kw.get("destination") else self.rows)
        for p in (patch.object(adapter, "search_flights", side_effect=search),
                  patch.object(FeizhuTailDiscoveryRunner, "_cancellable_wait", AsyncMock()),
                  patch("tail_discovery_service.asyncio.sleep", AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def run_service(self, **kw):
        return self.h.run_service(**{"max_baseline_queries": 0, **kw})

    def bundle(self, codes=("CAN", "SZX", "HGH")):
        return self.repo.tail_history_bundle(origin="BJS", transfer="SHA", candidates=list(codes))

    def test_filtered_price_still_records_route_not_matching_quote(self):
        result = self.run_service(max_price=500)
        self.assertEqual(result["saved"], 0)
        bundle = self.bundle()
        self.assertEqual(len(bundle["observations"]), 1)
        self.assertEqual(bundle["observations"][0]["quality"], "valid")
        self.assertEqual(bundle["outcomes"][0]["outcome"], "filtered")
        self.assertTrue(any(e["origin"] == "SHA" and e["destination"] == "CAN" for e in bundle["edges"]))

    def test_airline_and_cabin_filter_do_not_erase_structural_evidence(self):
        self.rows[0]["flight_details"][0].update(airline="MU", flight_no="MU1234", cabin="business")
        result = self.run_service(preferred_airlines=["CA"], cabin="economy")
        self.assertEqual(result["saved"], 0)
        self.assertEqual(self.bundle()["observations"][0]["quality"], "valid")
        self.assertEqual(self.bundle()["outcomes"][0]["outcome"], "filtered")

    def test_invalid_route_never_enters_graph(self):
        self.rows[0]["flight_details"][1]["departure_time"] = "10:00"
        self.run_service()
        self.assertEqual(self.bundle()["observations"], [])
        self.assertEqual(self.bundle()["edges"], [])
        self.assertEqual(self.bundle()["outcomes"][0]["outcome"], "unknown")

    def test_review_only_route_is_not_promoted_to_confirmed_graph(self):
        self.rows[0]["flight_details"][0].pop("arrival_date")
        self.run_service()
        self.assertEqual(self.bundle()["observations"][0]["quality"], "needs_review")
        self.assertEqual(self.bundle()["edges"], [])
        self.assertEqual(self.bundle()["outcomes"][0]["outcome"], "unknown")

    def test_unknown_cabin_is_not_a_failed_quote_preference(self):
        self.rows[0]["flight_details"][0].pop("cabin")
        self.run_service()
        self.assertEqual(self.bundle()["observations"][0]["quality"], "valid")
        self.assertEqual(self.bundle()["outcomes"][0]["outcome"], "unknown")

    def test_open_query_learns_real_cities_not_sentinel_or_omitted_cities(self):
        self.rows = [helpers.quote("CAN"), helpers.quote("SZX")]
        self.run_service(search_scope="open", candidates=[])
        bundle = self.bundle()
        self.assertEqual({r["destination"] for r in bundle["outcomes"]}, {"CAN", "SZX"})
        self.assertEqual({r["source_candidate"] for r in bundle["outcomes"]}, {OPEN_DESTINATION})
        self.assertEqual(len(bundle["legacy"]), 0)  # No double counting new attempts.

    def test_empty_open_response_does_not_create_negative_city_evidence(self):
        self.rows = []
        self.run_service(search_scope="open", candidates=[])
        self.assertEqual(self.bundle()["outcomes"], [])
        self.assertEqual(self.bundle()["observations"], [])

    def test_checkpoint_learning_is_atomic_on_storage_failure(self):
        with patch.object(self.repo, "insert_tail_discovery_attempt", side_effect=RuntimeError("disk")):
            with self.assertRaisesRegex(RuntimeError, "disk"):
                self.run_service(_job_id="atomic")
        bundle = self.bundle()
        self.assertEqual(bundle["observations"], [])
        self.assertEqual(bundle["outcomes"], [])
        self.assertEqual(bundle["edges"], [])
        self.assertEqual(self.repo.tail_checkpoints("atomic"), [])

    def test_completed_resume_does_not_add_learning_votes(self):
        first = self.run_service(_job_id="one")
        self.h.save_job("one", first)
        before = self.bundle()
        self.calls.clear()
        self.run_service(mode="resume", source_job_id="one")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bundle(), before)

    def test_initialization_is_additive_and_preserves_history(self):
        self.run_service()
        before = self.bundle()
        self.repo.initialize()
        self.assertEqual(self.bundle(), before)

    def test_resume_keeps_previous_priority_explanations(self):
        self.directed = {c: [helpers.quote(c)] for c in ("CAN", "SZX")}
        request = dict(candidates=["CAN", "SZX"], max_candidates=1)
        first = self.run_service(_job_id="one", **request)
        self.h.save_job("one", first, helpers.payload(**request))
        resumed = self.run_service(_job_id="two", mode="resume", source_job_id="one", **request)
        trace = resumed["candidate_policy"]["priority_trace"]
        self.assertEqual(len(trace), 2)
        self.assertEqual([r["job_id"] for r in trace], ["one", "two"])
        self.assertEqual(resumed["candidate_policy"]["known_route_edges"], 2)

    def test_old_graph_observation_cannot_replace_newer_last_seen(self):
        q = helpers.quote()
        self.repo.record_tail_route_edges(q)
        q["observed_at"] = "2020-01-01T00:00:00+00:00"
        self.repo.record_tail_route_edges(q)
        edge = self.bundle()["edges"][0]
        self.assertEqual(edge["first_observed_at"], "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(edge["last_observed_at"], edge["first_observed_at"])

    def test_historical_stale_graph_does_not_expand_hybrid_pool(self):
        q = helpers.quote("SZX")
        q["observed_at"] = "2020-01-01T00:00:00+00:00"
        self.repo.record_tail_route_edges(q)
        self.assertNotIn("SZX", self.repo.tail_history_destinations(origin="BJS", transfer="SHA"))

    def test_hybrid_open_then_bounded_real_city_followups(self):
        self.rows = [helpers.quote("CAN"), helpers.quote("SZX")]
        self.directed = {code: [helpers.quote(code)] for code in ("CAN", "SZX", "HGH")}
        result = self.run_service(search_scope="hybrid", candidates=["HGH"], max_followup_candidates=2)
        transit = [c["destination"] for c in self.calls if c["journey_type"] == "2"]
        self.assertIsNone(transit[0])
        self.assertEqual(len(transit), 3)
        self.assertEqual(len(set(transit[1:])), 2)
        self.assertNotIn(OPEN_DESTINATION, result["discovered_destinations"])
        self.assertEqual(result["stop_reason"], "followup_budget")
        self.assertGreater(result["candidate_policy"]["ranking_updates"], 2)
        self.assertEqual(result["metrics"]["provider_calls"]["used"], 4)
        self.assertTrue(result["candidate_policy"]["priority_trace"])
        self.assertLessEqual(result["metrics"]["valid_results_per_100_calls"], 75)

    def test_hybrid_pool_includes_recent_history_but_directed_stays_explicit(self):
        self.rows = [helpers.quote("SZX")]
        self.run_service(candidates=["SZX"])
        self.rows = []
        self.directed["SZX"] = [helpers.quote("SZX")]
        self.calls.clear()
        result = self.run_service(search_scope="hybrid", candidates=[], max_followup_candidates=1)
        self.assertIn("SZX", result["effective_candidates"])
        self.assertIn("SZX", [c["destination"] for c in self.calls])
        self.calls.clear()
        self.directed["CAN"] = [helpers.quote()]
        self.run_service(candidates=["CAN"])
        self.assertNotIn("SZX", [c["destination"] for c in self.calls])

    def test_hybrid_resume_never_repeats_successful_open_query(self):
        self.rows = [helpers.quote("CAN"), helpers.quote("SZX")]
        self.directed = {c: [helpers.quote(c)] for c in ("CAN", "SZX")}
        request = dict(search_scope="hybrid", candidates=[], max_followup_candidates=2, max_provider_calls=2)
        first = self.run_service(_job_id="one", **request)
        self.h.save_job("one", first, helpers.payload(**request, max_baseline_queries=0))
        self.assertEqual(first["stop_reason"], "provider_call_budget")
        self.calls.clear()
        resumed = self.run_service(mode="resume", source_job_id="one", max_provider_calls=10,
                                   **{k: v for k, v in request.items() if k != "max_provider_calls"})
        self.assertFalse(any(c["destination"] is None for c in self.calls))
        self.assertEqual(len([c for c in self.calls if c["journey_type"] == "2"]), 2)
        self.assertEqual(resumed["report"]["pending"], 0)
        self.assertEqual(len(self.repo.tail_checkpoints("one")), 3)

    def test_exhausted_hybrid_followup_cap_does_not_query_baseline_on_resume(self):
        self.rows = [helpers.quote("CAN"), helpers.quote("SZX")]
        request = dict(search_scope="hybrid", candidates=[], max_followup_candidates=0)
        first = self.run_service(_job_id="one", **request)
        self.h.save_job("one", first, helpers.payload(**request))
        self.calls.clear()
        self.run_service(mode="resume", source_job_id="one", **request)
        self.assertEqual(self.calls, [])

    def test_call_budget_includes_initial_baseline_and_errors(self):
        self.fail_destination = "CAN"
        result = self.run_service(candidates=["CAN", "SZX"], max_provider_calls=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["metrics"]["provider_calls"]["used"], 2)
        self.assertEqual(result["stop_reason"], "provider_call_budget")
        self.assertEqual(self.bundle()["outcomes"][0]["outcome"], "unknown")

    def test_one_call_budget_prioritizes_discovery_not_baseline(self):
        result = self.run_service(max_provider_calls=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["journey_type"], "2")
        self.assertEqual(result["saved"], 1)

    def test_call_budget_includes_enrichment_and_never_overshoots(self):
        self.rows = [helpers.quote(flight="CA1001"), helpers.quote(flight="CA1003")]
        result = self.run_service(max_provider_calls=3, max_baseline_queries=10)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["metrics"]["provider_calls"]["by_kind"]["same_flight_baseline"], 1)
        self.assertEqual(result["saved"], 2)

    def test_budget_stop_does_not_set_provider_cooldown(self):
        self.run_service(max_provider_calls=1)
        self.assertIsNone(self.repo.tail_cooldown("feizhu"))

    def test_actual_rate_limit_still_stops_hybrid_followups(self):
        async def limited(**kw):
            self.calls.append(kw)
            if kw["journey_type"] == "1":
                return []
            raise RuntimeError("429 rate limit")
        with patch.object(adapter, "search_flights", side_effect=limited):
            result = self.run_service(search_scope="hybrid", candidates=["CAN", "SZX"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertIsNotNone(self.repo.tail_cooldown("feizhu"))

    def test_ranking_uses_new_observations_during_same_task(self):
        # Discover SZX in open search; it moves ahead of unrelated cold CAN.
        self.rows = [helpers.quote("SZX")]
        self.directed["SZX"] = [helpers.quote("SZX")]
        result = self.run_service(search_scope="hybrid", candidates=["CAN"], max_followup_candidates=1)
        transit = [c["destination"] for c in self.calls if c["journey_type"] == "2"]
        self.assertEqual(transit, [None, "SZX"])
        self.assertIn("近期有条件相近的有效联程证据",
                      result["candidate_policy"]["priority_trace"][0]["reasons"])

    def test_manager_preserves_hybrid_seed_fingerprint_on_resume(self):
        manager = TailDiscoveryManager(config=SimpleNamespace(runtime_dir=Path(self.h.temp.name)),
            config_manager=None, repository=self.repo, run_lock=self.h.lock, safe_batch_size=3)
        manager.job_manager.get = Mock(return_value={
            "payload": helpers.payload(search_scope="hybrid", candidates=[]),
            "result": {"effective_candidates": [OPEN_DESTINATION, "CAN", "SZX"]}})
        manager.submit_tail_discovery = lambda value: value
        resumed = manager.restart_job("one", "resume")
        self.assertEqual(resumed["candidates"], [])
        self.assertEqual(resumed["search_scope"], "hybrid")

    def test_hybrid_request_validation(self):
        request = normalize_tail_discovery_request(helpers.payload(search_scope="hybrid", candidates=[]))
        self.assertEqual(request.max_provider_calls, 20)
        self.assertEqual(request.max_followup_candidates, 5)
        for changed in ({"provider": "ctrip"}, {"mode": "refresh"}, {"mode": "recheck"},
                        {"max_followup_candidates": -1}, {"max_followup_candidates": 51},
                        {"max_provider_calls": 1.5}, {"max_candidates": 1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                normalize_tail_discovery_request(helpers.payload(search_scope="hybrid", candidates=[], **changed))


class HistoryScoringTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        self.stamp = self.now.isoformat()
        self.context = history_context(normalize_tail_discovery_request(helpers.payload()))

    def outcome(self, destination="CAN", **kw):
        return {"destination": destination, "outcome": "matched", "context": deepcopy(self.context),
                "query_scope": "candidates", "observed_at": self.stamp, "elapsed_seconds": 10,
                "savings_amount": None, "currency": "CNY", **kw}

    def test_incomplete_or_network_errors_do_not_penalize_city(self):
        base = rank_candidates(["CAN", "SZX"], self.context, now=self.now)
        ranked = rank_candidates(["CAN", "SZX"], self.context,
                                 outcomes=[self.outcome(outcome="unknown")], now=self.now)
        self.assertEqual(base, ranked)

    def test_open_latency_not_misused_as_directed_city_query_cost(self):
        row = self.outcome(query_scope="open", elapsed_seconds=1)
        ranked = rank_candidates(["CAN"], self.context, outcomes=[row], now=self.now)
        self.assertEqual(ranked[0]["estimated_seconds"], 10)

    def test_context_provider_cabin_scope_passengers_airlines_and_time_matter(self):
        exact = context_similarity(self.context, self.context, self.stamp, self.now)
        for changes in ({"provider": "ctrip"}, {"cabin": "business"}, {"filter_scope": "all_legs"},
                        {"passengers": 2}, {"airlines": ["MU"]}, {"departure_time_start": "15:00"},
                        {"max_price": 100}, {"departure_date": "2027-06-21"}):
            with self.subTest(changes=changes):
                self.assertLess(context_similarity({**self.context, **changes}, self.context,
                                                   self.stamp, self.now), exact)

    def test_old_edges_decay_and_cannot_create_a_timeless_bonus(self):
        stale = {"destination": "SZX", "last_observed_at": (self.now - timedelta(days=100)).isoformat()}
        ranked = rank_candidates(["CAN", "SZX"], self.context, edges=[stale], now=self.now)
        self.assertEqual([r["destination"] for r in ranked], ["CAN", "SZX"])
        self.assertEqual(ranked[0]["score"], ranked[1]["score"])

    def test_same_query_many_fares_count_as_one_route_observation(self):
        row = {"destination": "CAN", "quality": "valid", "context": self.context,
               "observed_at": self.stamp, "root_job_id": "one", "source_candidate": OPEN_DESTINATION}
        one = rank_candidates(["CAN"], self.context, observations=[row], now=self.now)
        many = rank_candidates(["CAN"], self.context, observations=[row] * 100, now=self.now)
        self.assertEqual(one, many)

    def test_cross_provider_filtered_result_does_not_erase_positive_route_hints(self):
        # Context mismatch makes a filtered quote a very weak utility signal.
        other = self.outcome(outcome="filtered", context={**self.context, "provider": "ctrip"})
        same = self.outcome(outcome="filtered")
        ranked_other = rank_candidates(["CAN"], self.context, outcomes=[other], now=self.now)
        ranked_same = rank_candidates(["CAN"], self.context, outcomes=[same], now=self.now)
        self.assertGreater(ranked_other[0]["score"], ranked_same[0]["score"])

    def test_unknown_savings_is_not_fabricated_as_200_yuan(self):
        ranked = rank_candidates(["CAN"], self.context, outcomes=[self.outcome()], now=self.now)
        zero = rank_candidates(["CAN"], self.context, outcomes=[self.outcome(savings_amount=0)], now=self.now)
        self.assertEqual(ranked, zero)

    def test_exploration_slot_survives_dynamic_reranking(self):
        outcomes = [self.outcome("CAN"), self.outcome("SZX")]
        ranked = rank_candidates(["CAN", "SZX", "HGH"], self.context, outcomes=outcomes,
                                 now=self.now, position=1)
        self.assertEqual(ranked[0]["destination"], "HGH")
        self.assertIn("探索位：保留新城市查询机会", ranked[0]["reasons"])

    def test_future_evidence_does_not_leak_into_offline_replay(self):
        future = self.outcome(observed_at=(self.now + timedelta(days=1)).isoformat())
        ranked = rank_candidates(["CAN"], self.context, outcomes=[future], now=self.now)
        baseline = rank_candidates(["CAN"], self.context, now=self.now)
        self.assertEqual(ranked, baseline)

    def test_budget_counts_failed_attempt_and_blocks_before_io(self):
        budget = TailCallBudget(1)
        budget.consume("targeted_search")
        with self.assertRaises(ProviderCallBudgetExceeded):
            budget.consume("targeted_search")
        self.assertEqual(budget.used, 1)
