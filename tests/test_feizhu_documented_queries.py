"""Documented fields and bounded same-flight query regression tests."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from providers import feizhu_flyai_adapter as adapter
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from tail_quotes import apply_baselines, first_leg_key


def item():
    return json.loads((Path(__file__).parent / "fixtures/flyai_transit_trial.json").read_text())["data"]["itemList"][0]


class DocumentedFieldsTests(unittest.TestCase):
    def test_optional_destination_and_exact_flight(self):
        args = adapter._build_cli_args(origin="BJS", departure_date="2026-10-28",
                                       journey_type="2", transfer_city="CGO")
        self.assertNotIn("--destination", args)
        self.assertIn("郑州", args)
        args = adapter._build_cli_args(origin="BJS", destination="CGO", departure_date="2026-10-28",
                                       journey_type="1", transport_no="CA1915")
        self.assertEqual(args[args.index("--transport-no") + 1], "CA1915")

    def test_official_adult_price_and_duration_example(self):
        data = item()
        data.pop("ticketPrice")
        data["adultPrice"] = "¥1,400.50"
        data["journeys"][0]["segments"][0]["duration"] = "110分钟"
        offer = adapter._item_to_offer(data)
        self.assertEqual(offer["price"], 1400.5)
        self.assertEqual(offer["source_price_field"], "adultPrice")
        self.assertEqual(offer["source_price_value"], "¥1,400.50")
        self.assertEqual(offer["flight_details"][0]["duration_minutes"], 110)

    def test_do_not_extract_misleading_prices(self):
        for value in ("¥400起", "USD400", "400-500", "1,00", True, float("nan")):
            with self.subTest(value=value):
                data = item()
                data["ticketPrice"] = value
                data["adultPrice"] = "¥300"
                with self.assertRaises(ValueError):
                    adapter._item_to_offer(data)

    def test_ticket_price_precedence_preserves_source(self):
        data = item()
        data["adultPrice"] = "¥900"
        offer = adapter._item_to_offer(data)
        self.assertEqual(offer["price"], 300)
        self.assertEqual(offer["source_price_field"], "ticketPrice")

    def test_open_destination_response_does_not_mean_full_coverage(self):
        data = json.loads((Path(__file__).parent / "fixtures/flyai_open_destination.json").read_text())
        offers = [adapter._item_to_offer(i) for i in data["data"]["itemList"]]
        self.assertEqual(len(offers), 9)
        destinations = {o["flight_details"][-1]["destination"] for o in offers}
        self.assertEqual(len(destinations), 9)
        self.assertNotIn("HGH", destinations)  # Earlier targeted search did return HGH.


class TargetedBaselineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = FeizhuTailDiscoveryRunner(SimpleNamespace(default_currency="CNY"))
        self.offer = adapter._item_to_offer(item())
        self.result = {"provider": "feizhu", "price": 600, "cabin": "economy",
                       "flight_details": deepcopy(self.offer["flight_details"]), "raw_payload": {}}
        self.direct = deepcopy(self.offer)
        self.direct["flight_details"] = self.direct["flight_details"][:1]
        self.request = dict(origin="BJS", transfer="CGO", departure_date="2026-10-28",
                            cabin="economy", passengers=1)

    async def test_fill_missing_only_and_keep_market_separate(self):
        with patch.object(adapter, "search_flights", AsyncMock(side_effect=[[], [self.direct]])) as search, \
             patch.object(self.runner, "_cancellable_wait", AsyncMock()):
            catalog = await self.runner.query_baseline(**self.request)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=None)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=None)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(search.call_args.kwargs["transport_no"], "CA1915")
        self.assertIn(first_leg_key(self.result), catalog["same_flights"])
        self.assertIsNone(catalog["market"])
        apply_baselines([self.result], catalog)
        self.assertEqual(self.result["raw_payload"]["baselines"]["market"]["status"], "unavailable")
        self.assertEqual(self.result["raw_payload"]["baselines"]["same_flight"]["status"], "reference_only")

    async def test_existing_baseline_avoids_extra_query(self):
        with patch.object(adapter, "search_flights", AsyncMock(return_value=[self.direct])) as search:
            await self.runner.query_baseline(**self.request)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=None)
        self.assertEqual(search.await_count, 1)

    async def test_wrong_flight_not_accepted_and_failed_lookup_not_repeated(self):
        wrong = deepcopy(self.direct)
        wrong["flight_details"][0]["flight_no"] = "CA9999"
        with patch.object(adapter, "search_flights", AsyncMock(side_effect=[[], [wrong]])) as search, \
             patch.object(self.runner, "_cancellable_wait", AsyncMock()):
            catalog = await self.runner.query_baseline(**self.request)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=None)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=None)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(catalog["same_flights"], {})

    async def test_target_rate_limit_requests_stop(self):
        with patch.object(adapter, "search_flights", AsyncMock(side_effect=[[], RuntimeError("429 rate limit")])), \
             patch.object(self.runner, "_cancellable_wait", AsyncMock()):
            await self.runner.query_baseline(**self.request)
            attempt = {}
            stop = await self.runner._fill_same_flight_baseline([self.result], attempt=attempt, cancel_checker=None)
        self.assertTrue(stop)
        self.assertEqual(attempt["phase"], "rate_limited")

    async def test_max_one_extra_query_per_candidate(self):
        other = deepcopy(self.result)
        other["flight_details"][0]["flight_no"] = "CA1913"
        with patch.object(adapter, "search_flights", AsyncMock(side_effect=[[], [self.direct]])) as search, \
             patch.object(self.runner, "_cancellable_wait", AsyncMock()):
            await self.runner.query_baseline(**self.request)
            await self.runner._fill_same_flight_baseline([self.result, other], attempt={}, cancel_checker=None)
        self.assertEqual(search.await_count, 2)

    async def test_cancel_skips_extra_query(self):
        with patch.object(adapter, "search_flights", AsyncMock(return_value=[])) as search:
            await self.runner.query_baseline(**self.request)
            await self.runner._fill_same_flight_baseline([self.result], attempt={}, cancel_checker=lambda: True)
        self.assertEqual(search.await_count, 1)
