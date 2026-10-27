"""Open-destination discovery is one checkpoint, containing real destination rows."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from data_storage import PriceRepository
from providers import feizhu_flyai_adapter as adapter
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from tail_discovery_manager import TailDiscoveryManager
from tail_discovery_service import TailDiscoveryService
from tail_service import OPEN_DESTINATION, normalize_tail_discovery_request


def payload(**kw):
    return dict({"provider": "feizhu", "origin": "BJS", "transfer": "CGO",
                 "departure_date": "2026-10-28", "cabin": "economy", "search_scope": "open",
                 "candidates": [], "_job_id": "open1"}, **kw)


def offers():
    raw = json.loads((Path(__file__).parent / "fixtures/flyai_open_destination.json").read_text())
    return [adapter._item_to_offer(i) for i in raw["data"]["itemList"]]


class OpenRequestTests(unittest.TestCase):
    def test_no_candidate_selection_needed_and_ignored_in_open_scope(self):
        self.assertEqual(normalize_tail_discovery_request(payload()).candidates, [OPEN_DESTINATION])
        self.assertEqual(normalize_tail_discovery_request(payload(candidates=["HGH"])).candidates, [OPEN_DESTINATION])

    def test_unsupported_combinations_fail_before_query(self):
        for changes in ({"provider": "ctrip"}, {"mode": "refresh"}, {"mode": "recheck"},
                        {"search_scope": "bad"}, {"max_candidates": 1}, {"target_results": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_tail_discovery_request(payload(**changes))

    def test_newly_discovered_city_can_be_queried_directly(self):
        request = normalize_tail_discovery_request(payload(search_scope="candidates", candidates=["DAT", "DOY", "BZX"]))
        self.assertEqual(set(request.candidates), {"DAT", "DOY", "BZX"})


class OpenExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = PriceRepository(root / "test.sqlite")
        self.repo.initialize()
        self.lock = threading.Lock()
        self.config = SimpleNamespace(providers={"feizhu": SimpleNamespace(enabled=True)},
                                      default_currency="CNY", runtime_dir=root)
        self.config_manager = SimpleNamespace(load_credentials=lambda **kw: {})
        self.service = TailDiscoveryService(config=self.config, config_manager=self.config_manager,
            repository=self.repo, run_lock=self.lock, queue_file=root / "queue.json", safe_batch_size=3)
        self.responses = offers()
        self.calls = []

        async def search(**kw):
            self.calls.append(kw)
            return [] if kw.get("journey_type") == "1" else deepcopy(self.responses)
        self.search_patch = patch.object(adapter, "search_flights", side_effect=search)
        self.search_patch.start()
        self.addCleanup(self.search_patch.stop)
        self.wait_patch = patch.object(FeizhuTailDiscoveryRunner, "_cancellable_wait", AsyncMock())
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    async def run_open(self, **kw):
        return await self.service.run(payload(**kw))

    def save_job(self, result):
        self.repo.upsert_tail_discovery_job({"job_id": "open1", "status": "finished",
            "payload": payload(), "result": result, "progress": result["progress"]})

    async def test_open_query_saves_real_destinations_atomically(self):
        result = await self.run_open()
        transit_calls = [c for c in self.calls if c.get("journey_type") == "2"]
        self.assertEqual(len(transit_calls), 1)
        self.assertIsNone(transit_calls[0]["destination"])
        self.assertEqual(transit_calls[0]["transfer_city"], "CGO")
        rows = self.repo.fetch_tail_discovery_results()
        self.assertGreaterEqual(len(rows), 8)  # Do not truncate same-first-flight results to only three cities.
        self.assertNotIn(OPEN_DESTINATION, {r["destination"] for r in rows})
        checkpoints = self.repo.tail_checkpoints("open1")
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["destination"], OPEN_DESTINATION)
        self.assertEqual(len(checkpoints[0]["result_ids"]), len(rows))
        self.assertEqual(result["candidate_policy"]["progress_unit"], "开放查询批次")
        self.assertEqual(set(result["discovered_destinations"]), {r["destination"] for r in rows})

    async def test_finished_resume_does_not_query_again_or_fake_next_page(self):
        result = await self.run_open()
        self.save_job(result)
        self.calls.clear()
        resumed = await self.run_open(mode="resume", source_job_id="open1", _job_id="open2")
        self.assertEqual(self.calls, [])
        self.assertEqual(resumed["newly_saved"], 0)
        self.assertEqual(resumed["saved"], result["saved"])
        self.assertEqual(resumed["report"]["pending"], 0)
        self.assertEqual(resumed["progress"]["pending"], 0)

    async def test_failed_empty_query_is_retryable_and_not_a_negative_cache(self):
        self.responses = []
        result = await self.run_open()
        self.save_job(result)
        self.assertEqual(result["report"]["attempts"][0]["status"], "failed")
        self.responses = offers()
        resumed = await self.run_open(mode="resume", source_job_id="open1", _job_id="open2")
        self.assertGreater(resumed["saved"], 0)
        self.assertEqual(sum(c.get("journey_type") == "2" for c in self.calls), 2)

    async def test_price_filter_is_applied_to_open_response(self):
        result = await self.run_open(max_price=520)
        self.assertEqual({r["destination"] for r in result["results"]}, {"DAT", "DLC", "TAO"})
        self.assertTrue(all(r["price"] <= 520 for r in result["results"]))

    async def test_wrong_transfer_is_not_saved(self):
        self.responses[0]["flight_details"][0]["destination"] = "SHA"
        self.responses[0]["flight_details"][0]["arrival_airport"] = "SHA"
        result = await self.run_open()
        self.assertNotIn("DAT", result["discovered_destinations"])

    async def test_cancel_before_query_and_resume(self):
        result = await self.service.run(payload(), cancel_checker=lambda: True)
        self.assertEqual(result["saved"], 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.lock.locked())
        self.save_job(result)
        resumed = await self.run_open(mode="resume", source_job_id="open1")
        self.assertGreater(resumed["saved"], 0)

    async def test_timeout_does_not_mark_open_checkpoint_complete(self):
        async def slow(**kw):
            await asyncio.sleep(.1)
            return []
        with patch.object(adapter, "search_flights", side_effect=slow):
            result = await self.run_open(max_minutes=.0001)
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertFalse(any(c["status"] in {"matched", "no_match"} for c in self.repo.tail_checkpoints("open1")))
        self.assertFalse(self.lock.locked())

    async def test_changed_scope_cannot_reuse_open_checkpoint(self):
        result = await self.run_open()
        self.save_job(result)
        with self.assertRaises(ValueError):
            await self.run_open(mode="resume", source_job_id="open1", search_scope="candidates", candidates=["DAT"])

    async def test_transaction_rollback_keeps_whole_response_retryable(self):
        with patch.object(self.repo, "insert_tail_discovery_attempt", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                await self.run_open()
        self.assertEqual(self.repo.fetch_tail_discovery_results(), [])
        self.assertEqual(self.repo.tail_checkpoints("open1"), [])
        self.assertFalse(self.lock.locked())

    async def test_refresh_and_recheck_switch_to_actual_destinations(self):
        result = await self.run_open()
        self.save_job(result)
        manager = TailDiscoveryManager(config=self.config, config_manager=self.config_manager,
            repository=self.repo, run_lock=self.lock, safe_batch_size=3)
        manager.job_manager = SimpleNamespace(get=lambda _: {"status": "finished", "payload": payload(),
                                                             "result": result}, submit=lambda p: p)
        refreshed = manager.restart_job("open1", "refresh")
        self.assertEqual(refreshed["search_scope"], "candidates")
        self.assertEqual(set(refreshed["candidates"]), set(result["discovered_destinations"]))
        recheck = manager.recheck_result(self.repo.fetch_tail_discovery_results()[0]["id"])
        self.assertEqual(recheck["search_scope"], "candidates")
        self.assertEqual(len(recheck["candidates"]), 1)
        self.assertNotIn(OPEN_DESTINATION, recheck["candidates"])
