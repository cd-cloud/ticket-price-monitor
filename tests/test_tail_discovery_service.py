import tempfile
import threading
import unittest
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from data_storage import PriceRepository
from tail_discovery_service import TailDiscoveryService
from tail_service import tail_queue_key


class TailDiscoveryServiceTests(unittest.TestCase):
    def test_history_prioritization_keeps_all_explicit_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = PriceRepository(Path(tmp_dir) / "tail.db")
            repository.initialize()
            service = TailDiscoveryService(
                config=SimpleNamespace(),
                config_manager=SimpleNamespace(),
                repository=repository,
                run_lock=threading.Lock(),
                queue_file=Path(tmp_dir) / "queue.json",
                safe_batch_size=3,
            )
            candidates = ["SHA", "CAN", "CTU", "KMG", "XMN", "TAO", "HKG", "MFM"]

            prioritized = service._prioritize_candidates_with_history(
                candidates,
                origin="CKG",
                transfer="BJS",
                preferred_airlines=[],
            )

            self.assertEqual(set(candidates), set(prioritized))
            self.assertEqual(len(candidates), len(prioritized))

    def test_current_run_report_excludes_previous_queue_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = PriceRepository(Path(tmp_dir) / "tail.db")
            repository.initialize()
            queue_file = Path(tmp_dir) / "queue.json"
            candidates = ["CTU"]
            key = tail_queue_key(
                provider="ctrip",
                origin="BJS",
                transfer="SHA",
                departure_date="2026-06-20",
                cabin="economy",
                preferred_airlines=[],
                candidate_profile="",
                candidates=candidates,
                max_price=None,
                departure_time_start="",
                departure_time_end="",
            )
            queue_file.write_text(
                json.dumps(
                    {
                        "key": key,
                        "completed": [],
                        "failed": [],
                        "attempts": [
                            {
                                "destination": "CTU",
                                "status": "no_match",
                                "phase": "old",
                                "method": "old",
                                "error": "old attempt",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class Automation:
                def __init__(self, config, credentials):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                async def discover_tail_routes(self, provider_name, **kwargs):
                    return {
                        "results": [],
                        "errors": [],
                        "attempts": [
                            {
                                "destination": "CTU",
                                "status": "no_match",
                                "phase": "current",
                                "method": "test",
                                "error": "current attempt",
                            }
                        ],
                    }

            service = TailDiscoveryService(
                config=SimpleNamespace(
                    providers={"ctrip": SimpleNamespace(enabled=True)},
                    default_currency="CNY",
                ),
                config_manager=SimpleNamespace(load_credentials=lambda allow_missing=True: {}),
                repository=repository,
                run_lock=threading.Lock(),
                queue_file=queue_file,
                safe_batch_size=3,
            )

            with patch("browser_automation.BrowserAutomation", Automation):
                result = asyncio.run(
                    service.run(
                        {
                            "provider": "ctrip",
                            "origin": "BJS",
                            "transfer": "SHA",
                            "departure_date": "2026-06-20",
                            "cabin": "economy",
                            "candidates": candidates,
                        }
                    )
                )

            self.assertEqual(1, len(result["report"]["attempts"]))
            self.assertEqual("current", result["report"]["attempts"][0]["phase"])
            saved_queue = json.loads(queue_file.read_text(encoding="utf-8"))
            self.assertEqual(2, len(saved_queue["attempts"]))

    def test_feizhu_tail_discovery_does_not_start_browser_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = PriceRepository(Path(tmp_dir) / "tail.db")
            repository.initialize()
            service = TailDiscoveryService(
                config=SimpleNamespace(
                    providers={"feizhu": SimpleNamespace(enabled=True)},
                    default_currency="CNY",
                ),
                config_manager=SimpleNamespace(load_credentials=lambda allow_missing=True: {}),
                repository=repository,
                run_lock=threading.Lock(),
                queue_file=Path(tmp_dir) / "queue.json",
                safe_batch_size=3,
            )

            async def fake_search(*, origin, destination, departure_date, cabin, **kwargs):
                return [
                    {
                        "price": 888.0,
                        "flight_details": [
                            {
                                "segment_index": 1,
                                "origin": "BJS",
                                "destination": "SHA",
                                "departure_airport": "PEK",
                                "arrival_airport": "SHA",
                                "flight_no": "CA1001",
                                "airline": "CA",
                            },
                            {
                                "segment_index": 2,
                                "origin": "SHA",
                                "destination": "CAN",
                                "departure_airport": "SHA",
                                "arrival_airport": "CAN",
                                "flight_no": "CA1002",
                                "airline": "CA",
                            },
                        ],
                    }
                ]

            with (
                patch("providers.feizhu_flyai_adapter.search_flights", fake_search),
                patch("browser_automation.BrowserAutomation", side_effect=AssertionError("browser should not start")),
            ):
                result = asyncio.run(
                    service.run(
                        {
                            "provider": "feizhu",
                            "origin": "BJS",
                            "transfer": "SHA",
                            "departure_date": "2026-06-20",
                            "cabin": "economy",
                            "candidates": ["CAN"],
                        }
                    )
                )

            self.assertEqual(1, result["saved"])
            self.assertEqual(1, len(result["results"]))
            self.assertEqual("CAN", result["results"][0]["destination"])
            self.assertEqual(1, len(repository.fetch_tail_discovery_attempts(limit=10)))

    def test_reset_running_queue_items_after_cancel(self) -> None:
        queue_items = [
            {"destination": "BKK", "status": "running", "last_phase": "batch_started"},
            {"destination": "SIN", "status": "retryable_failed", "last_phase": "query_failed"},
        ]

        TailDiscoveryService._reset_running_queue_items(queue_items)

        self.assertEqual("pending", queue_items[0]["status"])
        self.assertEqual("cancelled_before_query", queue_items[0]["last_phase"])
        self.assertEqual("retryable_failed", queue_items[1]["status"])


if __name__ == "__main__":
    unittest.main()
