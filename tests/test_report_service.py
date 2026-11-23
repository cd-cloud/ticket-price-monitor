import json
import os
import tempfile
import unittest
from pathlib import Path

from config_manager import ConfigManager
from data_storage import PriceRepository, PriceSnapshot
from report_service import ReportService


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
                "providers": {
                    "ctrip": {
                        "enabled": True,
                        "login_url": "https://example.test/login",
                        "search_url_template": "https://example.test/{origin}-{destination}",
                        "storage_state_file": "./runtime/ctrip_state.json",
                    }
                },
                "routes": [],
            }
        ),
        encoding="utf-8",
    )


class ReportServiceTests(unittest.TestCase):
    def test_report_and_default_csv_export_create_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            config = ConfigManager(config_path=config_path).load()
            repository = PriceRepository(config.database_path)
            repository.initialize()
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
                    raw_payload={"best_offer": {"price": 880}},
                )
            )
            service = ReportService(config=config, repository=repository)

            paths = service.report()
            export_path = service.export_csv()

            self.assertTrue(Path(paths["csv_path"]).exists())
            self.assertTrue(Path(paths["markdown_path"]).exists())
            self.assertEqual({"csv_path", "markdown_path"}, set(paths))
            self.assertEqual(export_path.name, "price_history_export.csv")
            self.assertTrue(export_path.exists())
            if os.name != "nt":
                for path in (
                    config.database_path,
                    Path(paths["csv_path"]),
                    Path(paths["markdown_path"]),
                    export_path,
                ):
                    self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_lowest_report_row_does_not_borrow_currency_from_another_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            config = ConfigManager(config_path=config_path).load()
            repository = PriceRepository(config.database_path)
            repository.initialize()
            for price, currency, observed_at in (
                (100, "", "2026-05-13T00:00:00Z"),
                (200, "CNY", "2026-05-14T00:00:00Z"),
            ):
                repository.insert_snapshot(PriceSnapshot(
                    provider="ctrip", route_key="R", origin="SHA", destination="PEK",
                    departure_date="2026-06-01", return_date=None, cabin="economy",
                    passengers=1, currency=currency, price=price, observed_at=observed_at,
                    scraped_at=observed_at, notes=[], raw_payload={},
                ))

            paths = ReportService(config=config, repository=repository).report()
            markdown = Path(paths["markdown_path"]).read_text(encoding="utf-8")
            lowest_section = markdown.split("## Lowest Price By Provider", 1)[1]
            cheapest_line = next(
                line for line in lowest_section.splitlines()
                if line.startswith("| ctrip") and "100" in line
            )

            self.assertNotIn("CNY", cheapest_line)


if __name__ == "__main__":
    unittest.main()
