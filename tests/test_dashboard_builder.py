import unittest
from types import SimpleNamespace
from pathlib import Path

from config_manager import ProviderConfig, RouteQuery
from dashboard_builder import build_dashboard_payload


class DashboardBuilderTests(unittest.TestCase):
    def test_dashboard_includes_price_change_direction_for_latest_snapshot(self) -> None:
        route = RouteQuery(
            origin="PEK",
            destination="SHA",
            departure_date="2099-01-01",
            providers=["ctrip"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": ProviderConfig(
                    name="ctrip",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("state.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                )
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 1200,
                    "currency": "CNY",
                    "observed_at": "2026-05-16T10:00:00",
                    "scraped_at": "2026-05-16T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/oneway-pek0-sha0"},
                },
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 980,
                    "currency": "CNY",
                    "observed_at": "2026-05-17T10:00:00",
                    "scraped_at": "2026-05-17T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/oneway-pek0-sha0"},
                },
            ],
            report_paths={},
            last_run_summary={"errors": []},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
        )

        task = payload["tasks"][0]
        self.assertEqual(980, task["latest_price"])
        self.assertEqual(1200, task["previous_price"])
        self.assertEqual(-220, task["latest_price_delta_amount"])
        self.assertEqual("down", task["latest_price_delta_direction"])

    def test_error_status_hides_stale_latest_price(self) -> None:
        route = RouteQuery(
            origin="PUS",
            destination="BCN",
            departure_date="2026-11-14",
            return_date="2026-11-20",
            providers=["ctrip"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": ProviderConfig(
                    name="ctrip",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("state.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                )
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 236,
                    "currency": "CNY",
                    "observed_at": "2026-05-16T10:00:00",
                    "scraped_at": "2026-05-16T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/round-pus0-bcn0"},
                }
            ],
            report_paths={},
            last_run_summary={
                "errors": [
                    f"provider=ctrip route={route.route_key} failed: Ctrip returned no flights."
                ]
            },
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
            session_hints=[{"provider": "ctrip", "needs_login": True}],
        )

        task = payload["tasks"][0]
        self.assertEqual("error", task["status"])
        self.assertIsNone(task["latest_price"])
        self.assertEqual(236, task["stale_latest_price"])
        self.assertEqual([{"provider": "ctrip", "needs_login": True}], payload["session_hints"])

    def test_dashboard_excludes_providers_without_handlers_and_marks_expired_routes(self) -> None:
        route = RouteQuery(
            origin="PEK",
            destination="SHA",
            departure_date="2026-05-01",
            providers=["ctrip", "airchina"],
        )
        provider = ProviderConfig(
            name="ctrip",
            enabled=True,
            login_url="https://example.test/login",
            search_url_template="https://example.test/search",
            storage_state_file=Path("state.json"),
            selectors={},
            timeout_ms=1000,
            extra_headers={},
            min_delay_seconds=0,
            max_delay_seconds=0,
            locale="zh-CN",
            timezone="Asia/Shanghai",
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": provider,
                "airchina": ProviderConfig(
                    name="airchina",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("airchina.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                ),
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[],
            report_paths={},
            last_run_summary={"errors": []},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
        )

        self.assertEqual(["ctrip"], payload["provider_options"])
        self.assertEqual(1, len(payload["tasks"]))
        self.assertEqual("ctrip", payload["tasks"][0]["provider"])
        self.assertEqual("expired", payload["tasks"][0]["status"])
        self.assertTrue(payload["tasks"][0]["is_expired"])


if __name__ == "__main__":
    unittest.main()
