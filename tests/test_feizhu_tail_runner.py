"""Unit tests for the flyai-cli-based FeizhuTailDiscoveryRunner."""

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner


class FakeAutomation:
    """Minimal automation stand-in for the runner."""

    def __init__(self):
        self.app_config = SimpleNamespace(default_currency="CNY")


class FeizhuTailRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_empty_candidates(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())
        result = await runner.run(
            origin="CTU",
            transfer="HAK",
            departure_date="2026-06-20",
            cabin="economy",
            passengers=1,
            candidates=[],
        )
        self.assertEqual(result["results"], [])
        self.assertEqual(result["attempts"], [])

    async def test_run_no_match_when_api_returns_no_flights(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        # Patch the adapter so it returns nothing.
        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights
        adapter.search_flights = lambda **kw: asyncio.sleep(0) or []

        try:
            result = await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["SIN"],
            )
        finally:
            adapter.search_flights = original_search

        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "no_match")
        self.assertIn("No Feizhu itinerary", result["attempts"][0]["error"])

    async def test_run_matches_transfer_route(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights

        async def fake_search(*, origin, destination, departure_date, cabin, **kw):
            await asyncio.sleep(0)
            # Two offers: one direct (should be skipped), one via HAK (should match).
            return [
                {
                    "price": 1200.0,
                    "flight_details": [
                        {
                            "segment_index": 1,
                            "origin": "CTU",
                            "destination": "SIN",
                            "departure_airport": "TFU",
                            "arrival_airport": "SIN",
                            "flight_no": "3U3909",
                            "airline": "川航",
                        },
                    ],
                },
                {
                    "price": 1448.0,
                    "flight_details": [
                        {
                            "segment_index": 1,
                            "origin": "CTU",
                            "destination": "HAK",
                            "departure_airport": "TFU",
                            "arrival_airport": "HAK",
                            "flight_no": "HU7086",
                            "airline": "海航",
                        },
                        {
                            "segment_index": 2,
                            "origin": "HAK",
                            "destination": "SIN",
                            "departure_airport": "HAK",
                            "arrival_airport": "SIN",
                            "flight_no": "HU747",
                            "airline": "海航",
                        },
                    ],
                },
            ]

        adapter.search_flights = fake_search

        try:
            result = await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["SIN"],
            )
        finally:
            adapter.search_flights = original_search

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["price"], 1448.0)
        self.assertEqual(result["results"][0]["destination"], "SIN")
        self.assertEqual(len(result["results"][0]["flight_details"]), 2)

        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "matched")
        self.assertEqual(result["attempts"][0]["price"], 1448.0)

    async def test_run_filters_by_preferred_airlines(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights

        async def fake_search(*, origin, destination, departure_date, cabin, **kw):
            await asyncio.sleep(0)
            return [
                {
                    "price": 2000.0,
                    "flight_details": [
                        {
                            "segment_index": 1,
                            "origin": "CTU",
                            "destination": "HAK",
                            "departure_airport": "TFU",
                            "arrival_airport": "HAK",
                            "flight_no": "HU7086",
                            "airline": "海航",
                        },
                        {
                            "segment_index": 2,
                            "origin": "HAK",
                            "destination": "SIN",
                            "departure_airport": "HAK",
                            "arrival_airport": "SIN",
                            "flight_no": "HU747",
                            "airline": "海航",
                        },
                    ],
                },
            ]

        adapter.search_flights = fake_search

        try:
            result = await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["SIN"],
                preferred_airlines=["CA"],  # Only Air China — should not match Hainan
            )
        finally:
            adapter.search_flights = original_search

        self.assertEqual(len(result["results"]), 0)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["status"], "no_match")

    async def test_run_excludes_origin_and_transfer_from_candidates(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights
        call_log: list[dict] = []

        async def fake_search(*, origin, destination, departure_date, cabin, **kw):
            await asyncio.sleep(0)
            call_log.append({"origin": origin, "destination": destination})
            return []

        adapter.search_flights = fake_search

        try:
            result = await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["CTU", "HAK", "SIN", "PEK"],
            )
        finally:
            adapter.search_flights = original_search

        # CTU and HAK should have been removed from candidates.
        queried = {call["destination"] for call in call_log}
        self.assertNotIn("CTU", queried)
        self.assertNotIn("HAK", queried)
        self.assertIn("SIN", queried)
        self.assertIn("PEK", queried)

    async def test_progress_callback_fires(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights

        async def fake_search(*, origin, destination, departure_date, cabin, **kw):
            await asyncio.sleep(0)
            return [
                {
                    "price": 1448.0,
                    "flight_details": [
                        {
                            "segment_index": 1,
                            "origin": "CTU",
                            "destination": "HAK",
                            "departure_airport": "TFU",
                            "arrival_airport": "HAK",
                            "flight_no": "HU7086",
                            "airline": "海航",
                        },
                        {
                            "segment_index": 2,
                            "origin": "HAK",
                            "destination": "SIN",
                            "departure_airport": "HAK",
                            "arrival_airport": "SIN",
                            "flight_no": "HU747",
                            "airline": "海航",
                        },
                    ],
                },
            ]

        adapter.search_flights = fake_search

        events: list[dict] = []

        def capture(event: dict) -> None:
            events.append(event)

        try:
            await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["SIN"],
                progress_callback=capture,
            )
        finally:
            adapter.search_flights = original_search

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "candidate_finished")
        self.assertEqual(events[0]["destination"], "SIN")

    async def test_cancel_checker_stops_early(self) -> None:
        runner = FeizhuTailDiscoveryRunner(FakeAutomation())

        import providers.feizhu_flyai_adapter as adapter

        original_search = adapter.search_flights
        adapter.search_flights = lambda **kw: asyncio.sleep(0) or []

        try:
            result = await runner.run(
                origin="CTU",
                transfer="HAK",
                departure_date="2026-06-20",
                cabin="economy",
                passengers=1,
                candidates=["SIN", "PEK", "SHA"],
                cancel_checker=lambda: True,
            )
        finally:
            adapter.search_flights = original_search

        # Cancel fires before the first candidate is processed.
        self.assertEqual(len(result["attempts"]), 0)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("cancelled", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
