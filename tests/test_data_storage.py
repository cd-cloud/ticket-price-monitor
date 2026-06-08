import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from data_storage import PriceRepository, PriceSnapshot
from tail_models import TailDiscoveryAttempt, TailDiscoveryResult


class DataStorageTests(unittest.TestCase):
    def test_price_snapshot_quality_fields_round_trip_and_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "storage.db"
            repo = PriceRepository(db_path)
            repo.initialize()
            repo.insert_snapshot(
                PriceSnapshot(
                    provider="ctrip",
                    route_key="BJS|PAR|2026-06-01||economy|any|1",
                    origin="BJS",
                    destination="PAR",
                    departure_date="2026-06-01",
                    return_date=None,
                    cabin="economy",
                    passengers=1,
                    currency="CNY",
                    price=2880,
                    observed_at="2026-05-13T00:00:00Z",
                    scraped_at="2026-05-13T00:01:00Z",
                    notes=[],
                    raw_payload={"parser": "ctrip", "parser_confidence": "high"},
                    detail_quality="complete",
                    detail_source="network_exact",
                    browser_backend="chrome",
                )
            )

            row = repo.fetch_snapshots()[0]

            self.assertEqual(row["parser"], "ctrip")
            self.assertEqual(row["parser_confidence"], "high")
            self.assertEqual(row["detail_quality"], "complete")
            self.assertEqual(row["detail_source"], "network_exact")
            self.assertEqual(row["browser_backend"], "chrome")

    def test_initialize_migrates_existing_price_snapshot_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "storage.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE price_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider TEXT NOT NULL,
                        route_key TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        departure_date TEXT NOT NULL,
                        return_date TEXT,
                        cabin TEXT NOT NULL,
                        passengers INTEGER NOT NULL,
                        currency TEXT NOT NULL,
                        price REAL NOT NULL,
                        observed_at TEXT NOT NULL,
                        scraped_at TEXT NOT NULL,
                        notes TEXT,
                        raw_payload TEXT
                    )
                    """
                )

            repo = PriceRepository(db_path)
            repo.initialize()

            with closing(sqlite3.connect(db_path)) as conn:
                column_names = {row[1] for row in conn.execute("PRAGMA table_info(price_snapshots)").fetchall()}

            self.assertIn("parser_confidence", column_names)
            self.assertIn("detail_quality", column_names)
            self.assertIn("browser_backend", column_names)

    def test_tail_discovery_attempt_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "storage.db")
            repo.initialize()
            repo.insert_tail_discovery_attempt(
                {
                    "job_id": "job1",
                    "queue_key": "queue1",
                    "provider": "ctrip",
                    "origin": "BJS",
                    "transfer": "CTU",
                    "destination": "CAN",
                    "departure_date": "2026-06-01",
                    "cabin": "economy_plus",
                    "status": "failed",
                    "phase": "opening_results",
                    "method": "direct_url",
                    "error": "timeout",
                    "raw_payload": {"destination": "CAN"},
                }
            )

            rows = repo.fetch_tail_discovery_attempts(job_id="job1")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["destination"], "CAN")
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[0]["raw_payload"]["destination"], "CAN")

    def test_tail_discovery_models_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = PriceRepository(Path(tmp_dir) / "storage.db")
            repo.initialize()

            repo.insert_tail_discovery_attempt(
                TailDiscoveryAttempt(
                    job_id="job1",
                    queue_key="queue1",
                    origin="bjs",
                    transfer="ctu",
                    destination="can",
                    departure_date="2026-06-01",
                    cabin="economy_plus",
                    status="matched",
                    price=680,
                    raw_payload={"destination": "CAN"},
                )
            )
            repo.insert_tail_discovery_result(
                TailDiscoveryResult(
                    provider="ctrip",
                    origin="bjs",
                    transfer="ctu",
                    destination="can",
                    departure_date="2026-06-01",
                    cabin="economy_plus",
                    price=680,
                    observed_at="2026-05-13T00:00:00Z",
                    scraped_at="2026-05-13T00:01:00Z",
                    flight_details=[{"origin": "BJS", "destination": "CTU"}],
                    raw_payload={"detail_source": "network_exact"},
                )
            )

            attempts = repo.fetch_tail_discovery_attempts(job_id="job1")
            results = repo.fetch_tail_discovery_results(origin="BJS", transfer="CTU")

            self.assertEqual(attempts[0]["origin"], "BJS")
            self.assertEqual(attempts[0]["price"], 680)
            self.assertEqual(results[0]["destination"], "CAN")
            self.assertEqual(results[0]["flight_details"][0]["destination"], "CTU")
            self.assertEqual("BJS", results[0]["raw_payload"]["structured_itinerary"]["origin"])
            self.assertEqual("CTU", results[0]["raw_payload"]["structured_itinerary"]["legs"][0]["destination"])


if __name__ == "__main__":
    unittest.main()
