import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config_manager import ConfigConflictError, ConfigManager, RouteQuery, normalize_airport_code


class ConfigManagerTests(unittest.TestCase):
    def test_normalize_airport_code_supports_russian_city_aliases(self) -> None:
        self.assertEqual("IKT", normalize_airport_code("伊尔库茨克"))
        self.assertEqual("VVO", normalize_airport_code("海参崴"))
        self.assertEqual("VVO", normalize_airport_code("符拉迪沃斯托克"))

    def test_normalize_airport_code_supports_clean_airport_suffixes(self) -> None:
        self.assertEqual("PEK", normalize_airport_code("首都国际机场"))
        self.assertEqual("PVG", normalize_airport_code("浦东机场"))
        self.assertEqual("CTU", normalize_airport_code("成都双机场"))

    def test_normalize_airport_code_supports_new_domestic_destinations(self) -> None:
        self.assertEqual("JHG", normalize_airport_code("西双版纳"))
        self.assertEqual("JHG", normalize_airport_code("嘎洒国际机场"))
        self.assertEqual("TCZ", normalize_airport_code("腾冲"))
        self.assertEqual("TCZ", normalize_airport_code("驼峰机场"))
        self.assertEqual("CSX", normalize_airport_code("长沙"))
        self.assertEqual("CSX", normalize_airport_code("黄花国际机场"))

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

    def test_anti_detect_backend_config_requires_explicit_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps({
                    "storage": {"database_path": "./runtime/test.db"},
                    "defaults": {"browser_backend": "camoufox"},
                    "providers": {},
                    "routes": [],
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FLIGHT_TRACKER_ENABLE_ANTI_DETECT_BACKENDS", None)
                with self.assertRaisesRegex(ValueError, "disabled by default"):
                    ConfigManager(config_path=config_path).load()

            with patch.dict(
                os.environ,
                {"FLIGHT_TRACKER_ENABLE_ANTI_DETECT_BACKENDS": "1"},
                clear=False,
            ):
                config = ConfigManager(config_path=config_path).load()
                self.assertEqual("camoufox", config.browser_backend)

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

    def test_concurrent_save_cannot_be_overwritten_by_stale_load_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps({
                "storage": {"runtime_dir": "./runtime", "database_path": "./runtime/test.db"},
                "providers": {"ctrip": {"enabled": True}},
                "routes": [{
                    "origin": "SHA", "destination": "PEK",
                    "departure_date": "2099-01-01", "providers": ["ctrip"],
                }],
            }), encoding="utf-8")
            manager = ConfigManager(config_path=config_path)
            manager.load()
            manager._config_cache = None

            entered = threading.Event()
            release = threading.Event()
            save_started = threading.Event()
            errors = []
            original = manager._build_route

            def slow_build(item):
                entered.set()
                release.wait(2)
                return original(item)

            manager._build_route = slow_build

            def load_worker():
                try:
                    manager.load()
                except Exception as exc:
                    errors.append(exc)

            def save_worker():
                save_started.set()
                try:
                    manager.save_routes([RouteQuery(
                        origin="SHA", destination="CAN", departure_date="2099-01-02",
                        providers=["ctrip"],
                    )])
                except Exception as exc:
                    errors.append(exc)

            loader = threading.Thread(target=load_worker)
            loader.start()
            self.assertTrue(entered.wait(1))
            saver = threading.Thread(target=save_worker)
            saver.start()
            self.assertTrue(save_started.wait(1))
            time.sleep(0.05)
            release.set()
            loader.join(2)
            saver.join(2)

            self.assertFalse(errors)
            self.assertEqual("CAN", json.loads(config_path.read_text())["routes"][0]["destination"])
            self.assertEqual("CAN", manager.load().routes[0].destination)

    def test_sensitive_config_inputs_are_migrated_to_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_dir = root / "custom-state"
            state_dir.mkdir()
            state_path = state_dir / "ctrip.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}]}), encoding="utf-8")
            env_path = root / ".env"
            env_path.write_text("FLIGHT_TRACKER_MASTER_KEY=test\n", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "storage": {"runtime_dir": "./runtime", "database_path": "./runtime/test.db"},
                "providers": {"ctrip": {"storage_state_file": str(state_path)}},
                "routes": [],
            }), encoding="utf-8")
            for path in (state_path, env_path, config_path):
                os.chmod(path, 0o644)

            ConfigManager(config_path=config_path).load()

            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(env_path.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(config_path.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(state_path.stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(state_dir.stat().st_mode))

    def test_second_process_writer_detects_stale_config_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps({
                "storage": {"runtime_dir": "./runtime", "database_path": "./runtime/test.db"},
                "providers": {"ctrip": {"enabled": True}},
                "routes": [],
            }), encoding="utf-8")
            first = ConfigManager(config_path=config_path)
            second = ConfigManager(config_path=config_path)
            first.load()
            second.load()

            first.save_routes([RouteQuery(
                origin="SHA", destination="PEK", departure_date="2099-01-01",
                providers=["ctrip"],
            )])
            with self.assertRaises(ConfigConflictError):
                second.save_routes([RouteQuery(
                    origin="SHA", destination="CAN", departure_date="2099-01-02",
                    providers=["ctrip"],
                )])

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("PEK", saved["routes"][0]["destination"])


if __name__ == "__main__":
    unittest.main()
