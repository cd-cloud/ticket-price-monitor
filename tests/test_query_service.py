import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config_manager import ConfigManager
from data_storage import PriceRepository
from providers.base import ProviderQueryOutcome
from query_service import QueryService


def write_config(path: Path, *, include_group_routes: bool = False) -> None:
    routes = [
        {
            "origin": "SHA",
            "destination": "PEK",
            "departure_date": "2099-01-01",
            "cabin": "economy",
            "passengers": 1,
            "providers": ["ctrip", "disabled"],
        }
    ]
    if include_group_routes:
        routes.extend(
            [
                {
                    "origin": "SEL",
                    "destination": "PAR",
                    "departure_date": "2099-01-01",
                    "cabin": "economy",
                    "passengers": 1,
                    "providers": ["ctrip"],
                    "group_id": "rg_test",
                    "group_label": "Test group",
                },
                {
                    "origin": "CJU",
                    "destination": "BCN",
                    "departure_date": "2099-01-01",
                    "cabin": "economy",
                    "passengers": 1,
                    "providers": ["ctrip"],
                    "group_id": "rg_test",
                    "group_label": "Test group",
                },
            ]
        )
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
                    },
                    "disabled": {
                        "enabled": False,
                        "login_url": "https://example.test/login",
                        "search_url_template": "https://example.test/{origin}-{destination}",
                        "storage_state_file": "./runtime/disabled_state.json",
                    },
                    "feizhu": {
                        "enabled": True,
                        "timeout_ms": 1000,
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                        "storage_state_file": "./runtime/feizhu_state.json",
                    },
                },
                "routes": routes,
            }
        ),
        encoding="utf-8",
    )


class FakeAutomation:
    def __init__(self, *_args, **_kwargs) -> None:
        self.queries: list[tuple[str, str]] = []
        self.backend_override: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def query_provider(self, provider_name, route):
        self.queries.append((provider_name, route.route_key))
        return SimpleNamespace(
            provider=provider_name,
            route=route,
            price=880,
            currency="CNY",
            observed_at="2026-05-13T00:00:00Z",
            notes=["fake"],
            raw_payload={"best_offer": {"price": 880}},
        )


class SlowAutomation(FakeAutomation):
    async def query_provider(self, provider_name, route):
        await asyncio.sleep(1)
        return await super().query_provider(provider_name, route)


class QueryServiceTests(unittest.TestCase):
    def make_service(self, config_path: Path):
        manager = ConfigManager(config_path=config_path)
        config = manager.load()
        repository = PriceRepository(config.database_path)
        repository.initialize()
        summaries: list[dict] = []

        def refresh() -> None:
            nonlocal config
            config = manager.load()

        service = QueryService(
            config_manager=manager,
            repository=repository,
            run_lock=threading.Lock(),
            get_config=lambda: config,
            refresh_config=refresh,
            update_summary=summaries.append,
            automation_factory=lambda app_config, credentials: FakeAutomation(app_config, credentials),
        )
        return service, repository, summaries

    def test_run_once_saves_enabled_provider_snapshot_and_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, repository, summaries = self.make_service(config_path)

            result = asyncio.run(service.run_once())
            snapshots = repository.fetch_snapshots()

            self.assertEqual(result["saved"], 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["provider"], "ctrip")
            self.assertEqual(summaries[0]["running"], True)
            self.assertEqual(summaries[-1]["running"], False)

    def test_feizhu_provider_filter_uses_cli_without_browser_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["routes"][0]["providers"] = ["feizhu"]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            manager = ConfigManager(config_path=config_path)
            config = manager.load()
            repository = PriceRepository(config.database_path)
            repository.initialize()

            def fail_factory(_app_config, _credentials):
                raise AssertionError("browser automation should not start for Feizhu CLI queries")

            async def fake_query(_runtime, _provider, _route, _page, _response_store):
                return ProviderQueryOutcome(
                    price=520,
                    final_url="https://example.test/feizhu",
                    title="fake feizhu",
                    extra_payload={"parser": "fake_feizhu"},
                )

            service = QueryService(
                config_manager=manager,
                repository=repository,
                run_lock=threading.Lock(),
                get_config=lambda: config,
                refresh_config=lambda: None,
                automation_factory=fail_factory,
            )

            with patch("providers.feizhu.query", fake_query):
                result = asyncio.run(service.run_once(provider_filter="feizhu"))

            snapshots = repository.fetch_snapshots()
            self.assertEqual(result["saved"], 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(snapshots[0]["provider"], "feizhu")
            self.assertEqual(snapshots[0]["price"], 520)

    def test_provider_filter_can_skip_route_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, repository, _summaries = self.make_service(config_path)

            result = asyncio.run(service.run_once(provider_filter="priceline"))

            self.assertEqual(result["saved"], 0)
            self.assertEqual(result["errors"], [])
            self.assertEqual(repository.count_snapshots(), 0)

    def test_run_once_skips_expired_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["routes"][0]["departure_date"] = "2000-01-01"
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            service, repository, _summaries = self.make_service(config_path)

            result = asyncio.run(service.run_once())

            self.assertEqual(result["saved"], 0)
            self.assertEqual(result["errors"], [])
            self.assertEqual(repository.count_snapshots(), 0)

    def test_provider_filter_rejects_provider_without_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, repository, _summaries = self.make_service(config_path)

            result = asyncio.run(service.run_once(provider_filter="airchina"))

            self.assertEqual(result["saved"], 0)
            self.assertIn("Unknown provider", result["errors"][0])
            self.assertEqual(repository.count_snapshots(), 0)

    def test_backend_override_is_passed_to_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            manager = ConfigManager(config_path=config_path)
            config = manager.load()
            repository = PriceRepository(config.database_path)
            repository.initialize()
            created: list[FakeAutomation] = []

            def factory(app_config, credentials):
                automation = FakeAutomation(app_config, credentials)
                created.append(automation)
                return automation

            service = QueryService(
                config_manager=manager,
                repository=repository,
                run_lock=threading.Lock(),
                get_config=lambda: config,
                refresh_config=lambda: None,
                automation_factory=factory,
            )

            result = asyncio.run(service.run_once(provider_filter="ctrip", backend_override="camoufox"))

            self.assertEqual(result["saved"], 1)
            self.assertEqual("camoufox", created[0].backend_override)

    def test_provider_query_timeout_finishes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            manager = ConfigManager(config_path=config_path)
            config = manager.load()
            repository = PriceRepository(config.database_path)
            repository.initialize()
            summaries: list[dict] = []
            service = QueryService(
                config_manager=manager,
                repository=repository,
                run_lock=threading.Lock(),
                get_config=lambda: config,
                refresh_config=lambda: None,
                update_summary=summaries.append,
                automation_factory=lambda app_config, credentials: SlowAutomation(app_config, credentials),
                provider_query_timeout_seconds=0.01,
            )

            result = asyncio.run(service.run_once(provider_filter="ctrip"))

            self.assertEqual(result["saved"], 0)
            self.assertIn("query timed out", result["errors"][0])
            self.assertEqual(summaries[0]["running"], True)
            self.assertEqual(summaries[-1]["running"], False)

    def test_provider_query_timeout_scales_with_backend_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            manager = ConfigManager(config_path=config_path)
            config = manager.load()
            service = QueryService(
                config_manager=manager,
                repository=PriceRepository(config.database_path),
                run_lock=threading.Lock(),
                get_config=lambda: config,
                refresh_config=lambda: None,
            )

            timeout = service._provider_query_timeout_seconds(config, config.providers["ctrip"])
            overridden = service._provider_query_timeout_seconds(
                config,
                config.providers["ctrip"],
                backend_override="chrome",
            )

            self.assertEqual(timeout, 330.0)
            self.assertEqual(overridden, 150.0)

    def test_run_route_group_once_queries_only_group_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path, include_group_routes=True)
            service, repository, summaries = self.make_service(config_path)

            result = asyncio.run(service.run_route_group_once("rg_test", provider_filter="ctrip"))

            snapshots = repository.fetch_snapshots()
            self.assertEqual(result["saved"], 2)
            self.assertEqual(result["errors"], [])
            self.assertEqual(
                {"SEL|PAR|2099-01-01||economy|any|1", "CJU|BCN|2099-01-01||economy|any|1"},
                {item["route_key"] for item in snapshots},
            )
            self.assertEqual(summaries[0]["running"], True)
            self.assertEqual(summaries[-1]["running"], False)

    def test_run_route_group_once_reports_missing_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, repository, _summaries = self.make_service(config_path)

            result = asyncio.run(service.run_route_group_once("missing"))

            self.assertEqual(result["saved"], 0)
            self.assertIn("Route group not found", result["errors"][0])
            self.assertEqual(repository.count_snapshots(), 0)


if __name__ == "__main__":
    unittest.main()
