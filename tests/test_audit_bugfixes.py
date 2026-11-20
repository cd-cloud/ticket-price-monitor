import asyncio
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from config_manager import ConfigManager, RouteQuery
from dashboard_builder import build_dashboard_payload
from data_storage import PriceRepository, PriceSnapshot
from providers import feizhu, priceline
from providers.airchina_direct import flyclaw_records_from_report
from providers.airchina_parser import parse_reference_fares
from providers.base import ProviderQueryOutcome
from query_service import QueryService
from route_service import RouteService
from web_ui import create_app


def _write_test_config(path: Path) -> None:
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
                    "feizhu": {
                        "enabled": True,
                        "timeout_ms": 5000,
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                        "storage_state_file": "./runtime/feizhu_state.json",
                    },
                },
                "routes": [],
            }
        ),
        encoding="utf-8",
    )


class AuditBugfixRegressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("FLIGHT_TRACKER_UI_TOKEN", None)

    def test_ui_token_protects_get_endpoints_and_sets_session_cookie_from_query(self) -> None:
        os.environ["FLIGHT_TRACKER_UI_TOKEN"] = "strong-secret-token"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_test_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                # Health probe remains accessible without token.
                self.assertEqual(200, client.get("/api/health").status_code)
                # Sensitive GET endpoints require token when UI token is configured.
                self.assertEqual(401, client.get("/api/dashboard").status_code)
                self.assertEqual(401, client.get("/api/log").status_code)
                self.assertEqual(401, client.get("/download/csv").status_code)

                # Initial browser visit with ?token=... sets an HttpOnly session cookie.
                index_resp = client.get("/?token=strong-secret-token")
                self.assertEqual(200, index_resp.status_code)
                self.assertEqual(200, client.get("/api/dashboard").status_code)

    def test_cross_site_post_without_origin_is_blocked_by_sec_fetch_site_or_referer(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_test_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                blocked_fetch = client.post(
                    "/api/report",
                    headers={"Sec-Fetch-Site": "cross-site"},
                )
                blocked_referer = client.post(
                    "/api/report",
                    headers={"Referer": "https://attacker.example/csrf.html"},
                )
                self.assertEqual(403, blocked_fetch.status_code)
                self.assertEqual(403, blocked_referer.status_code)

    def test_route_key_distinguishes_preferred_airlines_and_prevents_silent_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_test_config(config_path)
            manager = ConfigManager(config_path=config_path)
            config = manager.load()

            def refresh() -> None:
                nonlocal config
                config = manager.load()

            service = RouteService(
                config_manager=manager,
                get_config=lambda: config,
                refresh_config=refresh,
                rebalance_auto_query=lambda _routes: None,
                auto_query_interval_hours=12,
            )
            any_airline = service.upsert_route(
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2099-05-01",
                    "providers": ["ctrip"],
                }
            )["route"]
            ca_only = service.upsert_route(
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2099-05-01",
                    "providers": ["ctrip"],
                    "preferred_airlines": ["CA"],
                }
            )["route"]

            self.assertNotEqual(any_airline["route_key"], ca_only["route_key"])
            self.assertEqual(2, len(service.list_routes()))

    def test_dashboard_falls_back_to_legacy_route_key_for_historical_airline_snapshots(self) -> None:
        route = RouteQuery(
            origin="SHA",
            destination="PEK",
            departure_date="2099-05-01",
            providers=["ctrip"],
            preferred_airlines=["CA"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={"ctrip": SimpleNamespace(enabled=True)},
            default_currency="CNY",
            timezone="Asia/Shanghai",
        )
        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[
                {
                    "provider": "ctrip",
                    "route_key": route.legacy_route_key,
                    "price": 760,
                    "currency": "CNY",
                    "observed_at": "2026-10-01T10:00:00Z",
                    "scraped_at": "2026-10-01T10:00:00Z",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/oneway-sha-pek"},
                }
            ],
            report_paths={},
            last_run_summary={"errors": []},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=10,
        )
        self.assertEqual(760, payload["tasks"][0]["latest_price"])

    def test_unfiltered_run_once_routes_feizhu_via_cli_without_browser_for_feizhu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_test_config(config_path)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["routes"] = [
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2099-06-01",
                    "providers": ["feizhu", "ctrip"],
                }
            ]
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            manager = ConfigManager(config_path=config_path)
            config = manager.load()
            repo = PriceRepository(config.database_path)
            repo.initialize()
            browser_providers_called: list[str] = []

            class FakeBrowserAutomation:
                def __init__(self, *_a, **_kw):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_a):
                    return None

                async def query_provider(self, provider_name, route):
                    browser_providers_called.append(provider_name)
                    return SimpleNamespace(
                        provider=provider_name,
                        route=route,
                        price=650,
                        currency="CNY",
                        observed_at="2026-11-20T00:00:00Z",
                        notes=[],
                        raw_payload={"parser": "fake_ctrip"},
                    )

            async def fake_feizhu_query(_runtime, _provider_cfg, _route, _page, _store):
                return ProviderQueryOutcome(
                    price=520,
                    final_url="https://www.fliggy.com/",
                    title="CA1501",
                    extra_payload={"parser": "feizhu_flyai_cli"},
                )

            service = QueryService(
                config_manager=manager,
                repository=repo,
                run_lock=threading.Lock(),
                get_config=lambda: config,
                refresh_config=lambda: None,
                automation_factory=FakeBrowserAutomation,
            )

            with patch("providers.feizhu.query", fake_feizhu_query):
                summary = asyncio.run(service.run_once())

            self.assertEqual(2, summary["saved"])
            self.assertEqual(["ctrip"], browser_providers_called)

    def test_feizhu_rejects_multi_passenger_and_mismatched_cabin_quotes(self) -> None:
        provider_cfg = SimpleNamespace(timeout_ms=5000, min_delay_seconds=0, max_delay_seconds=0)
        runtime = SimpleNamespace(throttle=lambda _cfg: asyncio.sleep(0))

        multi_pax_route = RouteQuery("SHA", "PEK", "2099-06-01", passengers=2)
        with self.assertRaisesRegex(RuntimeError, "single-passenger"):
            asyncio.run(feizhu.query(runtime, provider_cfg, multi_pax_route, None, []))

        premium_route = RouteQuery("SHA", "PEK", "2099-06-01", cabin="premium_economy", passengers=1)
        economy_only_offers = [
            {
                "price": 450.0,
                "final_url": "https://www.fliggy.com/",
                "title": "CA1501",
                "departure_time": "08:00",
                "row_text": "SHA->PEK | CA1501 | ¥450",
                "flight_details": [{"cabin": "经济舱", "origin": "SHA", "destination": "PEK"}],
            }
        ]
        with patch("providers.feizhu.feizhu_flyai_adapter.search_flights", return_value=economy_only_offers):
            with self.assertRaisesRegex(RuntimeError, "matched cabin"):
                asyncio.run(feizhu.query(runtime, provider_cfg, premium_route, None, []))

    def test_priceline_rejects_unverified_round_trip_instead_of_querying_one_way(self) -> None:
        provider_cfg = SimpleNamespace(name="priceline", timeout_ms=5000)
        runtime = SimpleNamespace(throttle=lambda _cfg: asyncio.sleep(0))
        round_trip = RouteQuery("JFK", "LAX", "2099-06-01", return_date="2099-06-08")
        with self.assertRaisesRegex(RuntimeError, "one-way queries only"):
            asyncio.run(priceline.query(runtime, provider_cfg, round_trip, None, []))

    def test_airchina_parser_and_flyclaw_bridge_support_overnight_and_cross_timezone_duration(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "airchina_visible_oneway.json"
        sample = json.loads(fixture_path.read_text(encoding="utf-8"))
        row = dict(sample["rows"][0])
        row["departure_time"] = "23:10"
        row["arrival_time"] = "01:25+1"
        row["duration"] = "2h15m"
        sample["rows"] = [row]

        route = RouteQuery("PEK", "SHA", "2026-11-10")
        parsed = parse_reference_fares(sample, route)
        self.assertEqual("reference_fares_observed", parsed["status"])
        offer = parsed["offers"][0]
        self.assertEqual("2026-11-11", offer["arrival_date"])
        self.assertEqual("01:25", offer["arrival_time"])
        self.assertEqual(135, offer["duration_minutes"])

        # Westbound international flight where local arrival clock is earlier than departure clock
        intl_route = RouteQuery("PEK", "LHR", "2099-11-10")
        intl_report = {
            "state": "reference_fares_observed",
            "reference_preview": {
                "offers": [
                    {
                        **offer,
                        "origin": "PEK",
                        "destination": "LHR",
                        "departure_date": "2099-11-10",
                        "arrival_date": "2099-11-10",
                        "departure_time": "14:30",
                        "arrival_time": "17:45",
                        "duration_minutes": 675,
                    }
                ]
            },
        }
        records = flyclaw_records_from_report(intl_report, intl_route)
        self.assertEqual(1, len(records))
        self.assertEqual(675, records[0]["duration_minutes"])

    def test_route_group_run_once_executes_single_consolidated_run(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_test_config(config_path)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["routes"] = [
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2099-06-01",
                    "providers": ["ctrip", "feizhu"],
                    "group_id": "rg_combo",
                    "group_label": "Combo",
                }
            ]
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            calls: list[tuple[str, str | None]] = []

            def fake_group_run(group_id: str, provider_filter: str | None = None):
                calls.append((group_id, provider_filter))
                return {"running": False, "saved": 2, "errors": []}

            with TestClient(create_app(str(config_path))) as client, \
                 patch("app_service.FlightPriceApplication.run_route_group_once_blocking", side_effect=fake_group_run), \
                 patch("app_service.FlightPriceApplication.report", return_value={}):
                resp = client.post("/api/route-groups/rg_combo/run-once")
                self.assertEqual(200, resp.status_code)

            self.assertEqual([("rg_combo", None)], calls)

    def test_sqlite_wal_and_recent_snapshots_window_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "prices.db"
            repo = PriceRepository(db_path)
            repo.initialize()
            with repo.connect() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual("wal", str(mode).lower())

            for idx, price in enumerate([900, 850, 800, 750, 700]):
                repo.insert_snapshot(
                    PriceSnapshot(
                        provider="ctrip",
                        route_key="SHA|PEK|2099-05-01||economy|any|1",
                        origin="SHA",
                        destination="PEK",
                        departure_date="2099-05-01",
                        return_date=None,
                        cabin="economy",
                        passengers=1,
                        currency="CNY",
                        price=float(price),
                        observed_at=f"2026-11-20T0{idx}:00:00Z",
                        scraped_at=f"2026-11-20T0{idx}:00:00Z",
                        notes=[],
                        raw_payload={"search_url": "https://flights.ctrip.com/online/list/oneway-sha-pek"},
                    )
                )

            window = repo.fetch_recent_snapshots_per_route(per_pair_limit=2)
            self.assertEqual(2, len(window))
            self.assertEqual([750.0, 700.0], [row["price"] for row in window])

    def test_init_config_shifts_expired_example_dates_into_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            manager = ConfigManager(config_path=config_path)
            manager.init_config()
            loaded = manager.load()
            self.assertTrue(loaded.routes)
            for route in loaded.routes:
                self.assertGreaterEqual(date.fromisoformat(route.departure_date), date.today())


if __name__ == "__main__":
    unittest.main()
