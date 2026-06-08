import json
import os
import tempfile
import unittest
from pathlib import Path

from config_manager import ConfigManager, normalize_airport_code


class ConfigManagerTests(unittest.TestCase):
    def test_normalize_airport_code_supports_russian_city_aliases(self) -> None:
        self.assertEqual("IKT", normalize_airport_code("伊尔库茨克"))
        self.assertEqual("VVO", normalize_airport_code("海参崴"))
        self.assertEqual("VVO", normalize_airport_code("符拉迪沃斯托克"))

    def test_explicit_config_uses_sibling_env_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "storage": {"database_path": "./runtime/test.db"},
                        "providers": {},
                        "routes": [],
                    }
                ),
                encoding="utf-8",
            )
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("FLIGHT_TRACKER_MASTER_KEY=from-sibling-env\n", encoding="utf-8")
            old_value = os.environ.pop("FLIGHT_TRACKER_MASTER_KEY", None)
            try:
                manager = ConfigManager(config_path=config_path)
                self.assertEqual(env_path, manager.env_path)
                self.assertEqual("from-sibling-env", os.environ.get("FLIGHT_TRACKER_MASTER_KEY"))
            finally:
                if old_value is not None:
                    os.environ["FLIGHT_TRACKER_MASTER_KEY"] = old_value
                else:
                    os.environ.pop("FLIGHT_TRACKER_MASTER_KEY", None)

    def test_chrome_default_adds_playwright_fallback_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "storage": {"database_path": "./runtime/test.db"},
                        "defaults": {"browser_backend": "chrome"},
                        "providers": {},
                        "routes": [],
                    }
                ),
                encoding="utf-8",
            )

            config = ConfigManager(config_path=config_path).load()

            self.assertEqual("chrome", config.browser_backend)
            self.assertEqual(["playwright"], config.browser_backend_fallbacks)

    def test_provider_locale_follows_accept_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
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
                        "defaults": {"locale": "zh-CN"},
                        "scheduler": {"timezone": "Asia/Shanghai"},
                        "providers": {
                            "example": {
                                "enabled": True,
                                "login_url": "https://example.test/login",
                                "search_url_template": "https://example.test/search",
                                "extra_headers": {"Accept-Language": "en-US,en;q=0.9"},
                            }
                        },
                        "routes": [],
                    }
                ),
                encoding="utf-8",
            )

            config = ConfigManager(config_path=config_path).load()

        self.assertEqual(config.providers["example"].locale, "en-US")
        self.assertEqual(config.providers["example"].timezone, "Asia/Shanghai")

    def test_provider_urls_are_optional_for_cli_backed_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
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
                        "providers": {
                            "feizhu": {
                                "enabled": True,
                                "timeout_ms": 60000,
                            }
                        },
                        "routes": [],
                    }
                ),
                encoding="utf-8",
            )

            config = ConfigManager(config_path=config_path).load()

        self.assertEqual("", config.providers["feizhu"].login_url)
        self.assertEqual("", config.providers["feizhu"].search_url_template)

    def test_route_provider_must_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
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
                        "defaults": {"locale": "zh-CN"},
                        "scheduler": {"timezone": "Asia/Shanghai"},
                        "providers": {
                            "ctrip": {
                                "enabled": True,
                                "login_url": "https://example.test/login",
                                "search_url_template": "https://example.test/search",
                            }
                        },
                        "routes": [
                            {
                                "origin": "SHA",
                                "destination": "PEK",
                                "departure_date": "2026-06-01",
                                "providers": ["missing"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown provider"):
                ConfigManager(config_path=config_path).load()

    def test_init_config_creates_local_config_from_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            manager = ConfigManager(config_path=config_path)

            created = manager.init_config()

            self.assertEqual(created, config_path)
            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("providers", payload)

    def test_init_config_refuses_to_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                ConfigManager(config_path=config_path).init_config()


if __name__ == "__main__":
    unittest.main()
