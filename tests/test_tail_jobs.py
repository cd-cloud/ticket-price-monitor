import tempfile
import threading
import time
import unittest
from pathlib import Path

from data_storage import PriceRepository
from tail_jobs import TailDiscoveryJobManager


class TailJobManagerTests(unittest.TestCase):
    def test_cancel_marks_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "jobs.db")
            repo.initialize()
            release = threading.Event()

            def runner(payload, progress_callback=None, cancel_checker=None):
                self.assertTrue(cancel_checker is not None)
                release.wait(timeout=2)
                return {
                    "running": False,
                    "saved": 0,
                    "errors": [],
                    "cancelled": bool(cancel_checker and cancel_checker()),
                    "progress": {"stage": "finished", "message": "done"},
                }

            manager = TailDiscoveryJobManager(runner, repo)
            job = manager.submit({"origin": "BJS"})
            cancelled = manager.cancel(job["job_id"])
            release.set()
            for _ in range(30):
                if manager.get(job["job_id"])["status"] in {"finished", "cancelled", "failed"}:
                    break
                time.sleep(0.05)

            self.assertIn(cancelled["status"], {"cancelling", "cancelled", "finished"})
            persisted = repo.fetch_tail_discovery_job(job["job_id"])
            self.assertIsNotNone(persisted)

    def test_recent_jobs_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "jobs.db")
            repo.initialize()

            def runner(payload, progress_callback=None, cancel_checker=None):
                return {"running": False, "saved": 0, "errors": []}

            manager = TailDiscoveryJobManager(runner, repo)
            job = manager.submit({"origin": "BJS"})
            for _ in range(30):
                if manager.get(job["job_id"])["status"] in {"finished", "cancelled", "failed"}:
                    break
                time.sleep(0.05)
            recent = manager.list_recent(limit=5)

            self.assertTrue(any(item["job_id"] == job["job_id"] for item in recent))

    def test_needs_verification_result_is_not_marked_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "jobs.db")
            repo.initialize()

            def runner(payload, progress_callback=None, cancel_checker=None):
                return {
                    "running": False,
                    "saved": 0,
                    "errors": ["verification detected"],
                    "progress": {"stage": "needs_verification", "message": "verify"},
                }

            manager = TailDiscoveryJobManager(runner, repo)
            job = manager.submit({"origin": "BJS"})
            for _ in range(30):
                if manager.get(job["job_id"])["status"] == "needs_verification":
                    break
                time.sleep(0.05)

            current = manager.get(job["job_id"])
            self.assertEqual(current["status"], "needs_verification")
            self.assertIsNone(current.get("finished_at"))

    def test_running_progress_clears_stale_needs_verification_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "jobs.db")
            repo.initialize()
            manager = TailDiscoveryJobManager(lambda payload: {}, repo)
            job = {
                "job_id": "job1",
                "status": "needs_verification",
                "created_at": "2026-05-18T00:00:00Z",
                "updated_at": "2026-05-18T00:00:00Z",
                "payload": {},
                "progress": {},
                "result": None,
                "error": None,
            }
            manager._jobs[job["job_id"]] = job
            callback = manager._progress_callback(job["job_id"])
            callback({"stage": "running", "message": "cooldown"})

            current = manager.get(job["job_id"])
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["progress"]["message"], "cooldown")

    def test_persisted_needs_verification_is_interrupted_after_restart(self) -> None:
        job = TailDiscoveryJobManager._normalize_persisted_job(
            {
                "job_id": "job1",
                "status": "needs_verification",
                "progress": {"stage": "needs_verification", "message": "verify"},
            }
        )

        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["progress"]["stage"], "interrupted")
        self.assertIn("请重新发起", job["progress"]["message"])

    def test_terminal_persisted_job_resets_running_queue_items(self) -> None:
        job = TailDiscoveryJobManager._normalize_persisted_job(
            {
                "job_id": "job1",
                "status": "cancelled",
                "progress": {
                    "queue_items": [
                        {"destination": "BKK", "status": "running"},
                        {"destination": "SIN", "status": "matched"},
                    ],
                    "queue": {
                        "items": [
                            {"destination": "KUL", "status": "running"},
                            {"destination": "HKT", "status": "no_match"},
                        ],
                        "counts": {"running": 1, "no_match": 1},
                    },
                    "diagnostics": {
                        "items": [
                            {"code": "failed", "label": "failed"},
                        ],
                    },
                },
            }
        )

        self.assertEqual(job["progress"]["queue_items"][0]["status"], "cancelled_before_query")
        self.assertEqual(job["progress"]["queue"]["items"][0]["status"], "cancelled_before_query")
        self.assertEqual(job["progress"]["queue"]["running"], 0)
        self.assertEqual(job["progress"]["queue"]["cancelled_before_query"], 1)
        self.assertEqual(job["progress"]["queue"]["no_match"], 1)
        self.assertNotIn("counts", job["progress"]["diagnostics"])


if __name__ == "__main__":
    unittest.main()
