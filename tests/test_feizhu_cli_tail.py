"""Offline regressions using a sanitized official CLI response captured 2026-10-27."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from data_storage import PriceRepository
from providers import feizhu_flyai_adapter as adapter
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from tail_discovery_service import TailDiscoveryService
from tail_quotes import public_quote_metadata

FIXTURE = Path(__file__).parent / "fixtures" / "flyai_transit_trial.json"


def response():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class OfficialCliTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, data, **kwargs):
        completed = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with patch.object(adapter, "_find_flyai_executable", return_value=["flyai"]), \
             patch.object(adapter, "_run_flyai_command_async", AsyncMock(return_value=completed)) as command:
            result = await adapter.search_flights(origin="BJS", destination="HGH",
                                                 departure_date="2026-10-28", **kwargs)
        return result, command.call_args.args[0]

    async def test_transfer_and_cabin_reach_real_command(self):
        offers, args = await self.search(response(), journey_type="2", transfer_city="CGO")
        self.assertEqual(args[args.index("--journey-type") + 1], "2")
        self.assertEqual(args[args.index("--transfer-city") + 1], "郑州")
        self.assertEqual(args[args.index("--seat-class-name") + 1], "economy")
        self.assertEqual(len(offers), 2)
        self.assertTrue(offers.search_metadata["trial_mode"])
        self.assertEqual(offers[0]["search_metadata"]["coverage"], "limited")
        self.assertEqual(offers[0]["flight_details"][1]["arrival_date"], "2026-10-29")

    async def test_empty_results_retain_trial_notice(self):
        data = response()
        data["data"]["itemList"] = []
        offers, _ = await self.search(data)
        self.assertEqual(offers, [])
        self.assertTrue(offers.search_metadata["trial_mode"])
        self.assertIn("体验模式", offers.search_metadata["system_message"])

    async def test_success_without_notice_does_not_promise_complete_inventory(self):
        data = response()
        data.pop("systemMessage")
        offers, _ = await self.search(data)
        self.assertEqual(offers.search_metadata["coverage"], "unknown")

    async def test_api_business_failure_not_treated_as_empty_search(self):
        for status, message in [(429, "rate limit"), (401, "invalid API key"), (500, "busy")]:
            with self.subTest(status=status):
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    await self.search({"status": status, "message": message, "data": {"itemList": []}})

    def test_price_and_one_way_journey_validation(self):
        item = response()["data"]["itemList"][0]
        for price in (None, "", "NaN", "Infinity", "-1", "0"):
            with self.subTest(price=price):
                candidate = deepcopy(item)
                candidate["ticketPrice"] = price
                with self.assertRaises(ValueError):
                    adapter._item_to_offer(candidate)
        candidate = deepcopy(item)
        candidate["journeys"].append(candidate["journeys"][0])
        with self.assertRaises(ValueError):
            adapter._item_to_offer(candidate)

    def test_direct_query_cannot_request_a_transfer(self):
        with self.assertRaises(ValueError):
            adapter._build_cli_args(origin="BJS", destination="HGH", departure_date="2026-10-28",
                                    journey_type="1", transfer_city="CGO")
        args = adapter._build_cli_args(origin="BJS", destination="HGH", departure_date="2026-10-28",
                                       cabin="business_first")
        self.assertEqual(args[args.index("--seat-class-name") + 1], "business,first")

    async def test_runner_routes_and_baseline_have_different_query_types(self):
        offers, _ = await self.search(response())
        runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        with patch.object(adapter, "search_flights", AsyncMock(return_value=offers)) as search:
            result = await runner.run(origin="BJS", transfer="CGO", departure_date="2026-10-28",
                                      cabin="economy", passengers=2, candidates=["HGH"])
            self.assertEqual(search.call_args.kwargs["journey_type"], "2")
            self.assertEqual(search.call_args.kwargs["transfer_city"], "CGO")
            self.assertEqual(len(result["results"]), 2)
            raw = result["results"][0]["raw_payload"]
            self.assertTrue(raw["search_metadata"]["trial_mode"])
            self.assertIsNone(raw["quote_terms"]["tax_included"])
            self.assertFalse(raw["quote_terms"]["inventory_verified"])
            self.assertEqual(raw["quote_terms"]["price_scope"], "unknown")
            self.assertIn("体验模式", public_quote_metadata(result["results"][0])["search_metadata"]["system_message"])
            await runner.query_baseline(origin="BJS", transfer="CGO", departure_date="2026-10-28",
                                        cabin="economy", passengers=2)
            self.assertEqual(search.call_args.kwargs["journey_type"], "1")
            self.assertNotIn("transfer_city", search.call_args.kwargs)

    async def test_runner_still_checks_actual_route(self):
        offers, _ = await self.search(response())
        runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        with patch.object(adapter, "search_flights", AsyncMock(return_value=offers)):
            result = await runner.run(origin="BJS", transfer="SHA", departure_date="2026-10-28",
                                      cabin="economy", passengers=1, candidates=["HGH"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["attempts"][0]["phase"], "search_incomplete")
        self.assertEqual(result["attempts"][0]["status"], "failed")

    async def test_incomplete_search_is_not_negative_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = PriceRepository(root / "db.sqlite")
            repo.initialize()
            config = SimpleNamespace(providers={"feizhu": SimpleNamespace(enabled=True)}, default_currency="CNY")
            runner = FeizhuTailDiscoveryRunner(config)
            service = TailDiscoveryService(
                config=config, config_manager=SimpleNamespace(load_credentials=lambda **kw: {}),
                repository=repo, run_lock=threading.Lock(), queue_file=root / "queue.json", safe_batch_size=3,
            )
            class Client:
                async def query_tail_baseline(self, provider, **kwargs):
                    return None
                async def discover_tail_routes(self, provider, **kwargs):
                    return await runner.run(**kwargs)
            @asynccontextmanager
            async def client(*args):
                yield Client()
            service._tail_discovery_client = client
            offers = adapter.FlightSearchResults(metadata={"trial_mode": True, "system_message": "体验模式"})
            with patch.object(adapter, "search_flights", AsyncMock(return_value=offers)) as search:
                request = {"provider": "feizhu", "origin": "BJS", "transfer": "CGO",
                           "departure_date": "2027-06-20", "cabin": "economy", "candidates": ["HGH"]}
                first = await service.run(request)
                second = await service.run(request)
            self.assertEqual(search.await_count, 2)
            self.assertEqual(first["report"]["attempts"][0]["phase"], "search_incomplete")
            self.assertEqual(second["report"]["attempts"][0]["status"], "failed")

    async def test_targeted_baseline_rate_limit_saves_quotes_and_cools_provider(self):
        offers, _ = await self.search(response())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = PriceRepository(root / "db.sqlite")
            repo.initialize()
            config = SimpleNamespace(providers={"feizhu": SimpleNamespace(enabled=True)}, default_currency="CNY")
            service = TailDiscoveryService(
                config=config, config_manager=SimpleNamespace(load_credentials=lambda **kw: {}),
                repository=repo, run_lock=threading.Lock(), queue_file=root / "queue.json", safe_batch_size=3,
            )
            with patch.object(adapter, "search_flights", AsyncMock(
                    side_effect=[[], offers, RuntimeError("429 rate limit")])) as search, \
                 patch.object(FeizhuTailDiscoveryRunner, "_cancellable_wait", AsyncMock()):
                result = await service.run({"provider": "feizhu", "origin": "BJS", "transfer": "CGO",
                    "departure_date": "2026-10-28", "cabin": "economy", "candidates": ["HGH"]})
            self.assertEqual(search.await_count, 3)
            self.assertEqual(len(repo.fetch_tail_discovery_results()), 2)
            self.assertEqual(result["stop_reason"], "rate_limited")
            self.assertTrue(repo.tail_cooldown("feizhu"))


if __name__ == "__main__":
    unittest.main()
