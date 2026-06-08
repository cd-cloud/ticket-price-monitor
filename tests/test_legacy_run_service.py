import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from config_manager import ConfigManager
from data_storage import PriceRepository, PriceSnapshot
from legacy_run_service import LegacyRunService
from run_summary import RunSummaryStore


def write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "storage": {
                    "runtime_dir": "./runtime",
                    "output_dir": "./output",
                    "database_path": "./runtime/test.db",
                    "encrypted_credentials_path": "./runtime/credentials.enc.json",
                    "salt_file": "./runtime/master.salt",
                    "log_file": "./runtime/test.log",
                },
                "defaults": {"locale": "zh-CN", "currency": "CNY"},
                "scheduler": {"timezone": "Asia/Shanghai"},
                "providers": {},
                "routes": [],
            }
        ),
        encoding="utf-8",
    )


class LegacyRunServiceTests(unittest.TestCase):
    def make_context(self, tmp_dir: str):
        config_path = Path(tmp_dir) / "config.json"
        write_config(config_path)
        config = ConfigManager(config_path=config_path).load()
        repository = PriceRepository(config.database_path)
        repository.initialize()
        run_summary = RunSummaryStore()
        return config, repository, run_summary

    def test_run_once_via_cli_counts_new_snapshots_and_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config, repository, run_summary = self.make_context(tmp_dir)

            def fake_runner(*_args, **_kwargs):
                repository.insert_snapshot(
                    PriceSnapshot(
                        provider="ctrip",
                        route_key="SHA|PEK|2026-06-01||economy|any|1",
                        origin="SHA",
                        destination="PEK",
                        departure_date="2026-06-01",
                        return_date=None,
                        cabin="economy",
                        passengers=1,
                        currency="CNY",
                        price=880,
                        observed_at="2026-05-13T00:00:00Z",
                        scraped_at="2026-05-13T00:01:00Z",
                        notes=[],
                        raw_payload={},
                    )
                )
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

            service = LegacyRunService(
                config=config,
                repository=repository,
                run_summary=run_summary,
                process_runner=fake_runner,
            )

            result = service.run_once_via_cli(provider_filter="ctrip")

            self.assertEqual(result["saved"], 1)
            self.assertEqual(result["stdout"], "ok")
            self.assertEqual(run_summary.get()["saved"], 1)

    def test_start_run_cycle_via_cli_uses_injected_spawner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config, repository, run_summary = self.make_context(tmp_dir)
            calls = []

            def fake_spawner(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(pid=4321)

            service = LegacyRunService(
                config=config,
                repository=repository,
                run_summary=run_summary,
                process_spawner=fake_spawner,
                platform_name="posix",
            )

            result = service.start_run_cycle_via_cli(provider_filter="ctrip")

            self.assertEqual(result["pid"], 4321)
            self.assertIn("run-cycle", calls[0][0])
            self.assertIn("--provider", calls[0][0])


if __name__ == "__main__":
    unittest.main()
